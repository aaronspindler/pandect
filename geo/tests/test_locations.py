import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import RestrictedError
from django.test import TestCase
from django.urls import reverse

from core.evidence.types import KINDS, LOCATION_KINDS
from core.testing import LOCATION_MODELS, EvidenceTestMixin, EvidenceViewTestMixin
from geo.models import PersonLocationEvidence, VehicleLocationEvidence
from people.models import Person


class LocationModelTests(EvidenceTestMixin, TestCase):
    def test_vehicle_speed_checksum_survives_postgresql_zero_normalization(self):
        for speed in (Decimal("-0.000"), Decimal("0.000"), Decimal("12.125"), None):
            with self.subTest(speed=speed):
                item = self.create_evidence(VehicleLocationEvidence, speed_kmh=speed)
                original = item.original_bytes()
                checksum = item.sha256
                item.refresh_from_db()
                self.assertEqual(item.speed_kmh, speed)
                self.assertEqual(item.original_bytes(), original)
                self.assertEqual(hashlib.sha256(original).hexdigest(), checksum)

    def test_address_coordinates_and_observation_time_are_retained_and_hashed(self):
        for model in LOCATION_MODELS:
            for address, latitude, longitude in (
                ("  Synthetic café\r\n", None, None),
                ("", Decimal("-0"), Decimal("0")),
                ("Synthetic place", Decimal("-90"), Decimal("180")),
                ("", Decimal("90"), Decimal("-180")),
            ):
                with self.subTest(model=model, latitude=latitude, longitude=longitude):
                    item = self.new_evidence(
                        model,
                        address=address,
                        latitude=latitude,
                        longitude=longitude,
                        observed_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
                        source="Synthetic witness",
                    )
                    item.full_clean()
                    item.save()
                    checksum = item.sha256
                    item.refresh_from_db()
                    self.assertEqual(item.address, address)
                    self.assertEqual(item.latitude, latitude)
                    self.assertEqual(item.longitude, longitude)
                    self.assertEqual(
                        checksum, hashlib.sha256(item.original_bytes()).hexdigest()
                    )
                    original = json.loads(item.original_bytes())
                    self.assertEqual(original["address"], address)
                    self.assertEqual(
                        original["observed_at"], "2026-01-02T03:04:00+00:00"
                    )
                    self.assertNotIn("source", original)
                    if model is PersonLocationEvidence:
                        self.assertEqual(original["person_id"], str(self.person.pk))

    def test_invalid_location_is_rejected_by_validation_and_normal_save(self):
        for model in LOCATION_MODELS:
            for values in (
                {"address": ""},
                {"address": " \n\t"},
                {"latitude": "1"},
                {"longitude": "1"},
                {"latitude": "90.000001", "longitude": "0"},
                {"latitude": "-90.000001", "longitude": "0"},
                {"latitude": "0", "longitude": "180.000001"},
                {"latitude": "0", "longitude": "-180.000001"},
                {"latitude": "NaN", "longitude": "0"},
                {"latitude": "0.1234567", "longitude": "0"},
            ):
                with self.subTest(model=model, values=values):
                    item = self.new_evidence(model, **values)
                    with self.assertRaises(ValidationError):
                        item.full_clean()
                    with self.assertRaises(ValidationError):
                        item.save()

    def test_database_enforces_coordinate_pair_ranges_and_location_presence(self):
        for model in LOCATION_MODELS:
            item = self.create_evidence(model)
            for values in (
                {"address": " \n"},
                {"latitude": 1},
                {"longitude": 1},
                {"latitude": 91, "longitude": 0},
                {"latitude": 0, "longitude": 181},
            ):
                with self.subTest(model=model, values=values):
                    with transaction.atomic(), self.assertRaises(IntegrityError):
                        model.objects.filter(pk=item.pk).update(**values)

    def test_original_location_fields_and_person_cannot_be_replaced(self):
        replacement = Person.objects.create(case=self.case, first_name="Another person")
        for model in LOCATION_MODELS:
            item = self.create_evidence(model)
            changes = {
                "address": "Replacement address",
                "latitude": Decimal("1"),
                "longitude": Decimal("1"),
                "observed_at": datetime(2026, 1, 2, tzinfo=UTC),
            }
            if model is PersonLocationEvidence:
                changes["person"] = replacement
            for field, value in changes.items():
                with self.subTest(model=model, field=field):
                    changed = model.objects.get(pk=item.pk)
                    setattr(changed, field, value)
                    with self.assertRaises(ValidationError):
                        changed.full_clean()
                    with self.assertRaises(ValidationError):
                        changed.save(update_fields=["description"])

    def test_person_must_exist_in_same_case_even_without_full_clean(self):
        other = Person.objects.create(case=self.other_case, first_name="Private person")
        for person in (None, other):
            item = self.new_evidence(PersonLocationEvidence, person=person)
            with self.assertRaises(ValidationError):
                item.full_clean()
            with self.assertRaises(ValidationError):
                item.save()

    def test_person_deletion_retains_evidence_and_case_deletion_removes_both(self):
        item = self.create_evidence(PersonLocationEvidence)
        other_person = Person.objects.create(case=self.other_case, first_name="Other")
        retained = self.create_evidence(
            PersonLocationEvidence, case=self.other_case, person=other_person
        )
        with self.assertRaises(RestrictedError):
            self.person.delete()
        self.case.delete()
        self.assertFalse(PersonLocationEvidence.objects.filter(pk=item.pk).exists())
        self.assertFalse(Person.objects.filter(pk=self.person.pk).exists())
        self.assertTrue(PersonLocationEvidence.objects.filter(pk=retained.pk).exists())


class LocationViewTests(EvidenceViewTestMixin, TestCase):
    def test_source_length_is_validated_for_every_evidence_type(self):
        self.login("owner")
        for kind, model in KINDS.items():
            response = self.client.post(
                self.url("create"),
                self.payload(kind, source="x" * 256),
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("source", response.json()["errors"])
            self.assertFalse(model.objects.exists())

    def test_location_forms_and_forged_person_choices(self):
        other = Person.objects.create(case=self.other_case, first_name="Private person")
        self.login("editor")
        for kind in LOCATION_KINDS:
            form = self.client.get(self.url("create"), {"kind": kind})
            self.assertContains(form, "Save location")
            self.assertNotContains(form, "data-evidence-upload")
            self.assertNotContains(form, 'name="file"')
            self.assertContains(form, 'name="source"')
            if kind == "person-location":
                self.assertContains(form, "Synthetic person")
                self.assertNotContains(form, "Private person")
        for person_id in (str(other.pk), "not-a-uuid", ""):
            response = self.client.post(
                self.url("create"),
                self.payload("person-location", person=person_id),
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("person", response.json()["errors"])
            self.assertNotContains(response, "Private person", status_code=400)
        self.assertFalse(PersonLocationEvidence.objects.exists())

    def test_location_download_archive_and_idempotent_retry_preserve_original(self):
        self.login("editor")
        for kind in LOCATION_KINDS:
            data = self.payload(kind, latitude="0", longitude="-79.123456")
            response = self.client.post(
                self.url("create"), data, HTTP_ACCEPT="application/json"
            )
            self.assertEqual(response.status_code, 201)
            item = KINDS[kind].objects.get(pk=response.json()["id"])
            download = self.client.get(self.url("download", item, kind=kind))
            self.assertEqual(hashlib.sha256(download.content).hexdigest(), item.sha256)
            self.assertEqual(json.loads(download.content)["latitude"], "0.000000")
            self.assertEqual(download["Content-Type"], "application/octet-stream")
            self.assertEqual(
                download["Content-Disposition"],
                f'attachment; filename="evidence-{item.pk}.json"',
            )
            self.assertIn("no-store", download["Cache-Control"])
            self.assertEqual(download["X-Content-Type-Options"], "nosniff")
            self.client.post(self.url("archive", item, kind=kind))
            retry = self.client.post(
                self.url("create"), data, HTTP_ACCEPT="application/json"
            )
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json()["id"], str(item.pk))
            self.login("viewer")
            self.assertEqual(
                self.client.get(self.url("download", item, kind=kind)).content,
                download.content,
            )
            self.login("editor")
            self.client.post(self.url("restore", item, kind=kind))
            item.refresh_from_db()
            self.assertFalse(item.is_archived)

    def test_location_roles_revocation_and_cross_case_requests(self):
        self.login("owner")
        for kind in LOCATION_KINDS:
            created = self.client.post(
                self.url("create"), self.payload(kind), HTTP_ACCEPT="application/json"
            )
            item = KINDS[kind].objects.get(pk=created.json()["id"])
            for role, status in (("viewer", 403), ("outsider", 404)):
                self.login(role)
                self.assertEqual(
                    self.client.post(
                        self.url("create"), self.payload(kind)
                    ).status_code,
                    status,
                )
                for action in ("edit", "archive", "restore"):
                    self.assertEqual(
                        self.client.post(self.url(action, item, kind=kind)).status_code,
                        status,
                    )
            self.login("owner")
            for action in ("detail", "download", "edit"):
                self.assertEqual(
                    self.client.get(
                        self.url(action, item, kind=kind, case=self.other_case)
                    ).status_code,
                    404,
                )
            self.case.memberships.filter(user=self.users["editor"]).delete()
            self.login("editor")
            self.assertEqual(
                self.client.get(self.url("download", item, kind=kind)).status_code, 404
            )
            self.login("owner")

    def test_source_edit_search_detail_and_location_original_preservation(self):
        self.login("editor")
        for kind, model in KINDS.items():
            created = self.client.post(
                self.url("create"), self.payload(kind), HTTP_ACCEPT="application/json"
            )
            item = model.objects.get(pk=created.json()["id"])
            checksum = item.sha256
            edited = self.client.post(
                self.url("edit", item, kind=kind),
                {
                    "kind": kind,
                    "title": item.title,
                    "source": f"Revised source {kind}",
                    "expected_edited_at": item.edited_at.isoformat(),
                    "address": "Replacement attempt",
                    "latitude": "90",
                    "person": str(self.person.pk),
                },
            )
            self.assertEqual(edited.status_code, 302, edited.content)
            item.refresh_from_db()
            self.assertEqual(item.source, f"Revised source {kind}")
            self.assertEqual(item.sha256, checksum)
            detail = self.client.get(
                self.url("detail", item, kind=kind), {"section": "details"}
            )
            self.assertContains(detail, f"Revised source {kind}")
            results = self.client.get(
                self.url("list"), {"type": kind, "q": item.source}
            )
            self.assertEqual(results.context["page_obj"].paginator.count, 1)
            if kind in LOCATION_KINDS:
                self.assertEqual(item.address, "  Synthetic harbour office\r\n")
                self.assertIsNone(item.latitude)
                self.assertContains(
                    self.client.get(
                        self.url("list"), {"type": kind, "q": "harbour office"}
                    ),
                    item.title,
                )
        results = self.client.get(self.url("list"), {"q": "Synthetic person"})
        self.assertEqual(results.context["page_obj"].paginator.count, 4)

    def test_person_delete_retains_location_evidence_and_case_counts_it(self):
        self.login("owner")
        for kind in LOCATION_KINDS:
            self.client.post(self.url("create"), self.payload(kind))
        results = self.client.get(self.url("list"))
        self.assertEqual(
            results.context["page_obj"].paginator.count, len(LOCATION_KINDS)
        )
        delete_url = reverse(
            "people:delete",
            kwargs={"case_id": self.case.pk, "person_id": self.person.pk},
        )
        item = PersonLocationEvidence.objects.get(case=self.case, person=self.person)
        blocked = self.client.get(delete_url)
        self.assertContains(blocked, self.url("detail", item, "person-location"))
        self.assertContains(blocked, "View all linked evidence")
        self.assertNotContains(blocked, "Permanently delete person")
        response = self.client.post(delete_url, {"confirm": "on"})
        self.assertContains(
            response, "linked to retained location evidence", status_code=400
        )
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())

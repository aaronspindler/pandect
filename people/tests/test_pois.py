from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from cases.models import CaseMembership
from cases.tests.test_map import MapCaseFixtures
from people.models import Person, PointOfInterest
from people.poi_forms import poi_revision


class PersonPointOfInterestTests(MapCaseFixtures, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_person = Person.objects.create(
            case=cls.other, first_name="Private person"
        )
        cls.poi = PointOfInterest.objects.create(
            person=cls.person,
            created_by=cls.owner,
            kind="home",
            latitude=0,
            longitude=0,
            notes="Unconfirmed residence",
        )
        cls.other_poi = PointOfInterest.objects.create(
            person=cls.other_person,
            created_by=cls.outsider,
            kind="custom",
            custom_label="Secret place",
            latitude=1,
            longitude=1,
        )

    def poi_url(self, action, poi=None, case=None):
        args = [(case or self.case).pk]
        if action != "create":
            args.append((poi or self.poi).pk)
        return reverse(f"people:poi_{action}", args=args)

    def data(self, **changes):
        return {
            "person": self.person.pk,
            "kind": "home",
            "custom_label": "",
            "latitude": "43.64",
            "longitude": "-79.38",
            "address": "Synthetic road",
            "source": "Synthetic citation",
            "notes": "Keep uncertainty",
            **changes,
        }

    def test_owner_and_editor_add_each_kind_with_attribution_and_private_details(self):
        for user in (self.owner, self.editor):
            self.client.force_login(user)
            for kind in ("home", "work", "custom"):
                with self.subTest(user=user, kind=kind):
                    response = self.client.post(
                        self.poi_url("create"),
                        self.data(
                            kind=kind,
                            custom_label="Gym",
                            case=self.other.pk,
                            created_by=self.outsider.pk,
                        ),
                    )
                    self.assertEqual(response.status_code, 302)
                    poi = PointOfInterest.objects.get(
                        pk=response.url.strip("/").split("/")[-1]
                    )
                    self.assertEqual(poi.person.case_id, self.case.pk)
                    self.assertEqual(poi.created_by, user)
                    self.assertEqual(
                        poi.label, "Gym" if kind == "custom" else kind.title()
                    )
                    detail = self.client.get(response.url)
                    self.assertContains(detail, "Investigator annotation")
                    self.assertContains(detail, "Synthetic citation")
                    self.assertIn("no-store", detail["Cache-Control"])

    def test_invalid_labels_coordinates_and_person_preserve_draft(self):
        self.client.force_login(self.editor)
        for values, field in (
            ({"kind": "custom", "custom_label": "   "}, "custom_label"),
            ({"kind": "invalid"}, "kind"),
            ({"latitude": ""}, "latitude"),
            ({"latitude": "NaN"}, "latitude"),
            ({"latitude": "90.000001"}, "latitude"),
            ({"longitude": "-180.000001"}, "longitude"),
            ({"latitude": "1.1234567"}, "latitude"),
            ({"person": self.other_person.pk}, "person"),
            ({"person": uuid4()}, "person"),
        ):
            with self.subTest(values=values):
                response = self.client.post(self.poi_url("create"), self.data(**values))
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.context["form"].errors)
                self.assertContains(response, "Keep uncertainty", status_code=400)
        self.assertEqual(PointOfInterest.objects.count(), 2)

    def test_prefills_are_case_scoped_and_empty_case_has_next_action(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            self.poi_url("create"),
            {
                "person": self.person.pk,
                "latitude": "0",
                "longitude": "-180",
            },
        )
        self.assertEqual(response.context["form"]["person"].value(), self.person.pk)
        self.assertEqual(response.context["form"]["latitude"].value(), "0")
        self.assertNotContains(response, "Private person")
        for value in (self.other_person.pk, "bad-uuid"):
            self.assertEqual(
                self.client.get(self.poi_url("create"), {"person": value}).status_code,
                404,
            )
        self.client.force_login(self.outsider)
        empty = self.other.__class__.objects.create(owner=self.outsider)
        self.assertContains(
            self.client.get(self.poi_url("create", case=empty)), "Add a person first"
        )

    def test_all_roles_read_map_person_and_detail_but_viewer_cannot_mutate(self):
        for user in (self.owner, self.editor, self.viewer):
            self.client.force_login(user)
            for url in (
                self.poi_url("detail"),
                reverse("people:detail", args=(self.case.pk, self.person.pk)),
                self.url_for_map(),
            ):
                response = self.client.get(url)
                self.assertContains(response, "Home")
                self.assertNotContains(response, "Secret place")
            self.assertEqual(self.client.post(self.poi_url("detail")).status_code, 405)
        self.assertNotContains(response, "Add POI")
        for action in ("create", "edit", "delete"):
            self.assertEqual(self.client.get(self.poi_url(action)).status_code, 403)
            self.assertEqual(
                self.client.post(
                    self.poi_url(action), self.data(confirm="on")
                ).status_code,
                403,
            )

    def url_for_map(self):
        return reverse("cases:case_map", args=(self.case.pk,))

    def test_outsiders_superusers_and_cross_case_ids_are_not_disclosed(self):
        for superuser in (False, True):
            self.outsider.is_superuser = superuser
            self.outsider.save()
            self.client.force_login(self.outsider)
            for action in ("create", "detail", "edit", "delete"):
                self.assertEqual(self.client.get(self.poi_url(action)).status_code, 404)
                if action != "detail":
                    self.assertEqual(
                        self.client.post(
                            self.poi_url(action), self.data(confirm="on")
                        ).status_code,
                        404,
                    )
        self.client.force_login(self.owner)
        for action in ("detail", "edit", "delete"):
            self.assertEqual(
                self.client.get(self.poi_url(action, self.other_poi)).status_code, 404
            )
            if action != "detail":
                self.assertEqual(
                    self.client.post(
                        self.poi_url(action, self.other_poi), self.data(confirm="on")
                    ).status_code,
                    404,
                )

    def test_authentication_email_verification_and_csrf(self):
        self.assertEqual(self.client.get(self.poi_url("create")).status_code, 302)
        self.owner.emailaddress_set.update(verified=False)
        self.client.force_login(self.owner)
        self.assertRedirects(
            self.client.get(self.poi_url("create")),
            reverse("account_email"),
            fetch_redirect_response=False,
        )
        self.owner.emailaddress_set.update(verified=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        for action in ("create", "edit", "delete"):
            self.assertEqual(
                client.post(self.poi_url(action), self.data(confirm="on")).status_code,
                403,
            )

    def test_edit_conflict_preserves_proposal_and_requires_review(self):
        self.client.force_login(self.editor)
        data = self.data(revision=poi_revision(self.poi), notes="My draft")
        self.poi.notes = "Newer saved details"
        self.poi.save()
        response = self.client.post(self.poi_url("edit"), data)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "Newer saved details", status_code=409)
        self.assertContains(response, "My draft", status_code=409)
        self.poi.refresh_from_db()
        self.assertEqual(self.poi.notes, "Newer saved details")
        data["revision"] = response.context["form"]["revision"].value()
        self.assertEqual(self.client.post(self.poi_url("edit"), data).status_code, 302)
        self.poi.refresh_from_db()
        self.assertEqual(self.poi.notes, "My draft")
        self.assertEqual(self.poi.created_by, self.owner)
        for token in ("", "tampered", poi_revision(self.other_poi)):
            self.assertEqual(
                self.client.post(
                    self.poi_url("edit"), self.data(revision=token)
                ).status_code,
                400,
            )

    def test_revoked_or_downgraded_access_blocks_open_forms(self):
        self.client.force_login(self.editor)
        data = self.data(revision=poi_revision(self.poi))
        membership = CaseMembership.objects.get(case=self.case, user=self.editor)
        membership.role = "viewer"
        membership.save()
        for action in ("create", "edit", "delete"):
            self.assertEqual(
                self.client.post(self.poi_url(action), data).status_code, 403
            )
        membership.delete()
        for action in ("create", "detail", "edit", "delete"):
            self.assertEqual(self.client.get(self.poi_url(action)).status_code, 404)
        for action in ("create", "edit", "delete"):
            self.assertEqual(
                self.client.post(self.poi_url(action), data).status_code, 404
            )
        self.assertEqual(
            self.client.get(
                self.url_for_map(), HTTP_ACCEPT="application/json"
            ).status_code,
            404,
        )

    def test_editor_deletes_only_after_confirmation_and_stale_edit_cannot_recreate(
        self,
    ):
        self.client.force_login(self.editor)
        data = self.data(revision=poi_revision(self.poi))
        edit_url = self.poi_url("edit")
        self.assertContains(self.client.get(self.poi_url("delete")), "Delete Home")
        self.assertEqual(self.client.post(self.poi_url("delete")).status_code, 400)
        self.assertTrue(PointOfInterest.objects.filter(pk=self.poi.pk).exists())
        self.assertEqual(
            self.client.post(self.poi_url("delete"), {"confirm": "on"}).status_code, 302
        )
        self.assertEqual(self.client.post(edit_url, data).status_code, 404)
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())
        self.location.refresh_from_db()
        self.assertEqual(self.location.source, "Synthetic source")

    def test_person_and_case_deletion_cascade_only_annotations(self):
        person = Person.objects.create(case=self.case, first_name="Unlinked")
        poi = PointOfInterest.objects.create(
            person=person, created_by=self.owner, latitude=0, longitude=0
        )
        self.client.force_login(self.owner)
        case_deletion = self.client.get(
            reverse("cases:case_delete", args=(self.case.pk,))
        )
        self.assertEqual(case_deletion.context["poi_count"], 2)
        self.assertContains(case_deletion, "Person POIs")
        response = self.client.get(
            reverse("people:delete", args=(self.case.pk, person.pk))
        )
        self.assertContains(response, "1 POI")
        person.delete()
        self.assertFalse(PointOfInterest.objects.filter(pk=poi.pk).exists())
        self.assertTrue(PointOfInterest.objects.filter(pk=self.poi.pk).exists())
        self.case.delete()
        self.assertFalse(PointOfInterest.objects.filter(pk=self.poi.pk).exists())
        self.assertTrue(PointOfInterest.objects.filter(pk=self.other_poi.pk).exists())

    def test_database_constraints_reject_invalid_direct_writes(self):
        for values in (
            {"kind": "custom", "custom_label": " "},
            {"kind": "other"},
            {"latitude": 91},
            {"longitude": -181},
        ):
            with (
                self.subTest(values=values),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                PointOfInterest.objects.filter(pk=self.poi.pk).update(**values)
        for value in ("NaN", "Infinity", "-Infinity"):
            self.poi.latitude = Decimal(value)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.poi.full_clean()

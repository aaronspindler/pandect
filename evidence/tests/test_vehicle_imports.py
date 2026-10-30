import csv
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.deletion import RestrictedError
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from cases.models import Case
from evidence.forms import evidence_form
from evidence.models import (
    DocumentEvidence,
    VehicleLocationEvidence,
    VehicleLocationImport,
)
from evidence.tests.test_views import EvidenceViewTestMixin
from evidence.vehicle_imports import HEADERS, MAX_ROWS, parse_csv
from vehicles.models import Vehicle


def csv_content(*rows, **changes):
    """Synthetic fixtures only; never copy the user's asset/location identifiers."""
    row = {
        "Asset": "Synthetic van",
        "Serial Number": "000000000000123",
        "Address": "  10 Example Street, Example City, Ontario, CAN  ",
        "Speed": "0 km/h",
        "Heading": "NW",
        "Trigger": "Ignition On: parked time",
        "Time Summary": "51m",
        "Created Time": "2026-09-16, 2:57:22 PM",
        "Received Time": "2026-09-16, 2:57:25 PM",
    } | changes
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, HEADERS)
    writer.writeheader()
    writer.writerows(rows or [row])
    return output.getvalue().encode("utf-8")


class VehicleCSVParserTests(SimpleTestCase):
    def test_quoted_csv_bom_and_original_values_survive_interpretation(self):
        row = parse_csv(b"\xef\xbb\xbf" + csv_content(), "America/Toronto")[0]
        self.assertFalse(row.errors)
        self.assertEqual(row.number, 2)
        self.assertEqual(row.serial, "000000000000123")
        self.assertEqual(row.values["address"], row.raw["Address"])
        self.assertTrue(row.values["address"].startswith("  "))
        self.assertEqual(row.raw["Time Summary"], "51m")
        self.assertEqual(row.values["speed_kmh"], 0)
        self.assertEqual(
            row.values["observed_at"], datetime(2026, 9, 16, 18, 57, 22, tzinfo=UTC)
        )
        self.assertEqual(
            row.values["received_at"], datetime(2026, 9, 16, 18, 57, 25, tzinfo=UTC)
        )
        self.assertNotIn("latitude", row.values)

    def test_parser_bounds_headers_encoding_and_malformed_input(self):
        for content in (
            b"",
            b"x" * (1024 * 1024 + 1),
            b"\xff",
            b"Asset,Address\nx,y\n",
            b"Asset," * 8 + b"Asset\n",
            csv_content().replace(b"Synthetic van", b"Synthetic\x00van"),
            csv_content().splitlines()[0] + b'\n"unterminated',
        ):
            with self.subTest(content=content[:30]), self.assertRaises(ValidationError):
                parse_csv(content, "UTC")
        row = parse_csv(csv_content(), "UTC")[0].raw
        with self.assertRaises(ValidationError):
            parse_csv(csv_content(*([row] * (MAX_ROWS + 1))), "UTC")
        self.assertEqual(
            len(parse_csv(csv_content(*([row] * MAX_ROWS)), "UTC")), MAX_ROWS
        )

    def test_invalid_fields_and_dst_times_require_correction(self):
        for values in (
            {"Address": " \n"},
            {"Asset": "", "Serial Number": ""},
            {"Speed": "-1 km/h"},
            {"Speed": "5 mph"},
            {"Heading": "up"},
            {"Created Time": "not a date"},
            {"Received Time": "not a date"},
            {"Created Time": "2026-11-01, 1:30:00 AM"},
            {"Created Time": "2026-03-08, 2:30:00 AM"},
            {"Asset": "x" * 256},
        ):
            with self.subTest(values=values):
                self.assertTrue(
                    parse_csv(csv_content(**values), "America/Toronto")[0].errors
                )
        row = parse_csv(
            csv_content(**{"Speed": "", "Heading": "", "Received Time": ""}), "UTC"
        )[0]
        self.assertFalse(row.errors)
        self.assertIsNone(row.values["speed_kmh"])
        self.assertIsNone(row.values["received_at"])
        malformed = csv_content() + b"short,row\n"
        self.assertTrue(parse_csv(malformed, "UTC")[-1].errors)


class VehicleImportTests(EvidenceViewTestMixin, TestCase):
    def upload_url(self, case=None):
        return reverse(
            "evidence:vehicle_import_upload", kwargs={"case_id": (case or self.case).pk}
        )

    def review_url(self, batch, case=None):
        return reverse(
            "evidence:vehicle_import_review",
            kwargs={"case_id": (case or self.case).pk, "import_id": batch.pk},
        )

    def upload(self, content=None, **changes):
        return self.client.post(
            self.upload_url(),
            {
                "file": SimpleUploadedFile(
                    "synthetic-assets.csv",
                    content if content is not None else csv_content(),
                    "text/csv",
                ),
                "submission_id": str(uuid4()),
                "timezone": "America/Toronto",
                "source": "Synthetic export",
                "use_created_time": "on",
            }
            | changes,
        )

    def new_batch(self, content=None):
        self.login("editor")
        response = self.upload(content)
        self.assertEqual(response.status_code, 302, response.content)
        return VehicleLocationImport.objects.latest("created_at")

    def complete(self, batch, **changes):
        return self.client.post(
            self.review_url(batch),
            {
                "vehicle_0": str(self.vehicle.pk),
                "confirm": "on",
            }
            | changes,
        )

    def test_upload_preview_complete_and_repeat_preserve_source_and_provenance(self):
        batch = self.new_batch()
        original = csv_content()
        self.assertEqual(batch.document.sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(batch.document.file.read(), original)
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        preview = self.client.get(self.review_url(batch))
        self.assertContains(preview, "16 Sep 2026, 18:57:22")
        self.assertContains(preview, "000000000000123")
        self.assertContains(preview, "51m")
        self.assertEqual(self.complete(batch).status_code, 302)
        self.assertEqual(self.complete(batch).status_code, 302)
        item = VehicleLocationEvidence.objects.get()
        self.assertEqual(item.vehicle, self.vehicle)
        self.assertEqual(item.case, self.case)
        self.assertEqual(item.source_row, 2)
        self.assertEqual(item.source, "Synthetic export")
        self.assertTrue(item.title.endswith("18:57:22 UTC"))
        self.assertEqual(item.uploaded_by, self.users["editor"])
        self.assertEqual(item.speed_kmh, 0)
        self.assertIsNone(item.collected_at)
        self.assertIsNone(item.latitude)
        self.assertEqual(
            item.observed_at, datetime(2026, 9, 16, 18, 57, 22, tzinfo=UTC)
        )
        self.assertEqual(
            item.received_at, datetime(2026, 9, 16, 18, 57, 25, tzinfo=UTC)
        )
        original_json = json.loads(item.original_bytes())
        self.assertEqual(
            original_json["import"]["document_sha256"], batch.document.sha256
        )
        self.assertEqual(original_json["import"]["timezone"], "America/Toronto")
        self.assertEqual(original_json["raw_row"]["Serial Number"], "000000000000123")
        batch.refresh_from_db()
        self.assertIsNotNone(batch.completed_at)
        self.assertEqual(batch.vehicle_mapping, {"0": str(self.vehicle.pk)})
        batch.timezone = "UTC"
        with self.assertRaises(ValidationError):
            batch.save()
        for role in ("owner", "viewer"):
            self.login(role)
            download = self.client.get(
                self.url("download", item, kind="vehicle-location")
            )
            self.assertEqual(download.content, item.original_bytes())
            self.assertIn("no-store", download["Cache-Control"])
            self.assertContains(
                self.client.get(self.review_url(batch)), "Vehicle locations imported"
            )
        self.assertEqual(hashlib.sha256(item.original_bytes()).hexdigest(), item.sha256)

    def test_every_asset_group_requires_an_explicit_same_case_mapping(self):
        first = parse_csv(csv_content(), "UTC")[0].raw
        second = first | {"Asset": "Other asset", "Serial Number": "other"}
        batch = self.new_batch(csv_content(first, second))
        other_vehicle = Vehicle.objects.create(
            case=self.other_case, model="Hidden vehicle"
        )
        self.assertNotContains(
            self.client.get(self.review_url(batch)), "Hidden vehicle"
        )
        for changes in (
            {},
            {"vehicle_1": str(other_vehicle.pk)},
            {"vehicle_1": "invalid"},
            {"vehicle_1": str(self.vehicle.pk), "confirm": ""},
        ):
            self.assertEqual(self.complete(batch, **changes).status_code, 400)
            self.assertFalse(VehicleLocationEvidence.objects.exists())
        self.assertEqual(
            self.complete(batch, vehicle_1=str(self.vehicle.pk)).status_code, 302
        )
        self.assertEqual(VehicleLocationEvidence.objects.count(), 2)
        self.vehicle.refresh_from_db()
        self.assertEqual(self.vehicle.vin, "")
        self.assertEqual(self.vehicle.model, "Synthetic van")

    def test_malformed_rows_remain_reviewable_without_partial_import(self):
        first = parse_csv(csv_content(), "UTC")[0].raw
        batch = self.new_batch(csv_content(first, first | {"Speed": "invalid"}))
        self.assertContains(
            self.client.get(self.review_url(batch)), "Some rows need correction"
        )
        self.assertEqual(self.complete(batch).status_code, 400)
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        self.assertTrue(DocumentEvidence.objects.filter(pk=batch.document_id).exists())
        with patch.object(
            VehicleLocationEvidence,
            "save",
            side_effect=ValidationError("synthetic failure"),
        ):
            valid = self.new_batch()
            self.assertEqual(self.complete(valid).status_code, 400)
        valid.refresh_from_db()
        self.assertIsNone(valid.completed_at)

    def test_atomic_rollback_after_a_later_row_fails(self):
        first = parse_csv(csv_content(), "UTC")[0].raw
        batch = self.new_batch(csv_content(first, first | {"Heading": "E"}))
        save = VehicleLocationEvidence.save
        calls = []

        def fail_second(item, *args, **kwargs):
            calls.append(item)
            if len(calls) == 2:
                raise IntegrityError("synthetic failure")
            return save(item, *args, **kwargs)

        with patch.object(VehicleLocationEvidence, "save", fail_second):
            self.assertEqual(self.complete(batch).status_code, 400)
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        batch.refresh_from_db()
        self.assertIsNone(batch.completed_at)
        self.assertEqual(self.complete(batch).status_code, 302)
        self.assertEqual(VehicleLocationEvidence.objects.count(), 2)

    def test_possible_duplicates_require_review_within_and_between_exports(self):
        row = parse_csv(csv_content(), "UTC")[0].raw
        batch = self.new_batch(csv_content(row, row))
        self.assertContains(
            self.client.get(self.review_url(batch)), "Possible duplicate"
        )
        self.assertEqual(self.complete(batch).status_code, 400)
        self.assertEqual(self.complete(batch, confirm_duplicates="on").status_code, 302)
        another = self.new_batch()
        self.assertContains(
            self.client.get(self.review_url(another)), "Possible duplicate"
        )
        self.assertEqual(self.complete(another).status_code, 400)
        self.assertEqual(
            self.complete(another, confirm_duplicates="on").status_code, 302
        )
        self.assertEqual(VehicleLocationEvidence.objects.count(), 3)

    def test_largest_supported_import_completes_atomically(self):
        row = parse_csv(csv_content(), "UTC")[0].raw
        batch = self.new_batch(csv_content(*([row] * MAX_ROWS)))
        self.assertEqual(self.complete(batch, confirm_duplicates="on").status_code, 302)
        self.assertEqual(batch.observations.count(), MAX_ROWS)
        batch.refresh_from_db()
        self.assertIsNotNone(batch.completed_at)

    def test_upload_retry_cannot_change_file_timezone_or_uploader(self):
        self.login("editor")
        submission = str(uuid4())
        self.assertEqual(self.upload(submission_id=submission).status_code, 302)
        self.assertEqual(self.upload(submission_id=submission).status_code, 302)
        self.assertEqual(VehicleLocationImport.objects.count(), 1)
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertEqual(
            self.upload(submission_id=submission, timezone="UTC").status_code, 409
        )
        self.assertEqual(
            self.upload(
                csv_content(**{"Asset": "Changed"}), submission_id=submission
            ).status_code,
            409,
        )
        self.login("owner")
        self.assertEqual(self.upload(submission_id=submission).status_code, 409)

    def test_current_permissions_csrf_and_case_boundaries_on_both_steps(self):
        batch = self.new_batch()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.users["owner"])
        self.assertEqual(csrf.post(self.upload_url(), {}).status_code, 403)
        self.assertEqual(csrf.post(self.review_url(batch), {}).status_code, 403)
        for role, read, write in (
            ("owner", 200, 302),
            ("viewer", 200, 403),
            ("outsider", 404, 404),
        ):
            self.login(role)
            self.assertEqual(self.client.get(self.review_url(batch)).status_code, read)
            self.assertEqual(self.complete(batch).status_code, write)
            self.assertEqual(
                self.client.get(self.upload_url()).status_code,
                200 if role == "owner" else write,
            )
        self.login("editor")
        self.case.memberships.filter(user=self.users["editor"]).update(role="viewer")
        self.assertEqual(self.complete(batch).status_code, 403)
        self.case.memberships.filter(user=self.users["editor"]).delete()
        self.assertEqual(self.client.get(self.review_url(batch)).status_code, 404)
        self.assertEqual(self.complete(batch).status_code, 404)
        self.users["outsider"].is_superuser = True
        self.users["outsider"].save()
        self.login("outsider")
        self.assertEqual(self.client.get(self.review_url(batch)).status_code, 404)
        self.assertEqual(
            self.client.get(self.review_url(batch, self.other_case)).status_code, 404
        )

    def test_file_failures_are_recoverable_and_upload_rollback_removes_orphans(self):
        self.login("owner")
        for content in (b"invalid", b"x" * (1024 * 1024 + 1)):
            self.assertEqual(self.upload(content).status_code, 400)
        for changes in (
            {"timezone": ""},
            {"timezone": "Missing/Zone"},
            {"use_created_time": ""},
        ):
            self.assertEqual(self.upload(**changes).status_code, 400)
        self.assertFalse(DocumentEvidence.objects.exists())
        with patch.object(
            VehicleLocationImport,
            "save",
            side_effect=ValidationError("synthetic failure"),
        ):
            self.assertEqual(self.upload().status_code, 503)
        self.assertFalse(DocumentEvidence.objects.exists())
        self.assertEqual([p for p in self.media_root.rglob("*") if p.is_file()], [])
        batch = self.new_batch()
        batch.document.file.storage.delete(batch.document.file.name)
        self.assertEqual(self.client.get(self.review_url(batch)).status_code, 503)
        self.assertEqual(self.complete(batch).status_code, 503)
        self.assertFalse(VehicleLocationEvidence.objects.exists())

    def test_source_integrity_and_parser_version_are_checked_before_completion(self):
        batch = self.new_batch()
        with batch.document.file.storage.open(
            batch.document.file.name, "wb"
        ) as original:
            original.write(csv_content(**{"Heading": "E"}))
        self.assertEqual(self.complete(batch).status_code, 503)
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        VehicleLocationImport.objects.filter(pk=batch.pk).update(
            parser_version="unsupported"
        )
        self.assertEqual(self.client.get(self.review_url(batch)).status_code, 503)

    def test_uncertain_upload_commit_retains_potentially_referenced_original(self):
        self.login("owner")
        real_atomic = transaction.atomic
        outer = True

        @contextmanager
        def uncertain_commit(*args, **kwargs):
            with real_atomic(*args, **kwargs):
                yield
            raise OSError("Uncertain commit outcome")

        def atomic_context(*args, **kwargs):
            nonlocal outer
            if outer:
                outer = False
                return uncertain_commit(*args, **kwargs)
            return real_atomic(*args, **kwargs)

        with patch(
            "evidence.import_views.transaction.atomic", side_effect=atomic_context
        ):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        batch = VehicleLocationImport.objects.get()
        self.assertTrue(batch.document.file.storage.exists(batch.document.file.name))
        self.assertNotContains(response, "Uncertain commit outcome", status_code=503)

    def test_vehicle_detail_excludes_inconsistent_import_source_links(self):
        batch = self.new_batch()
        self.assertEqual(self.complete(batch).status_code, 302)
        item = VehicleLocationEvidence.objects.get(import_batch=batch)
        detail_url = reverse(
            "vehicles:detail",
            kwargs={"case_id": self.case.pk, "vehicle_id": self.vehicle.pk},
        )
        self.assertContains(self.client.get(detail_url), item.title)
        # Trusted maintenance writes can bypass model validation. Readers must
        # still apply the same source boundary as private evidence downloads.
        DocumentEvidence.objects.filter(pk=batch.document_id).update(
            case=self.other_case
        )
        self.assertNotContains(self.client.get(detail_url), item.title)

    def test_smaller_operator_limits_are_displayed_and_enforced(self):
        self.login("owner")
        with self.settings(PANDECT_MAX_FILE_BYTES=100):
            page = self.client.get(self.upload_url())
            self.assertEqual(page.context["max_csv_bytes"], 100)
            self.assertEqual(self.upload().status_code, 400)
        self.assertFalse(DocumentEvidence.objects.exists())

    def test_batch_rejects_cross_case_sources_and_duplicate_row_numbers(self):
        batch = self.new_batch()
        invalid = VehicleLocationImport(
            case=self.other_case,
            uploaded_by=batch.uploaded_by,
            document=batch.document,
            timezone="UTC",
        )
        with self.assertRaises(ValidationError):
            invalid.save()
        self.complete(batch)
        with self.assertRaises(IntegrityError), transaction.atomic():
            item = batch.observations.get()
            item.pk = uuid4()
            item._state.adding = True
            item.save()

    def test_retained_vehicle_links_immutable_fields_and_whole_case_cleanup(self):
        batch = self.new_batch()
        self.complete(batch)
        item = VehicleLocationEvidence.objects.get()
        for values in (
            {"vehicle_id": uuid4()},
            {"raw_row": {}},
            {"speed_kmh": 10},
            {"source_row": 3},
            {"received_at": None},
            {"import_batch": None},
        ):
            changed = VehicleLocationEvidence.objects.get(pk=item.pk)
            for name, value in values.items():
                setattr(changed, name, value)
            with self.assertRaises(ValidationError):
                changed.save(update_fields=("description",))
        with self.assertRaises(RestrictedError):
            self.vehicle.delete()
        with self.assertRaises(RestrictedError):
            batch.document.delete()
        self.login("owner")
        delete_url = reverse(
            "vehicles:delete",
            kwargs={"case_id": self.case.pk, "vehicle_id": self.vehicle.pk},
        )
        self.assertContains(
            self.client.post(delete_url, {"confirm": "on"}),
            "linked to retained location evidence",
            status_code=400,
        )
        self.client.post(self.url("archive", item, kind="vehicle-location"))
        self.assertContains(
            self.client.post(delete_url, {"confirm": "on"}),
            "linked to retained location evidence",
            status_code=400,
        )
        source_path = batch.document.file.name
        source_storage = batch.document.file.storage
        with self.captureOnCommitCallbacks(execute=True):
            self.case.delete()
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        self.assertFalse(VehicleLocationImport.objects.exists())
        self.assertFalse(source_storage.exists(source_path))

    def test_manual_and_imported_observations_reject_cross_case_links(
        self,
    ):
        other = Vehicle.objects.create(case=self.other_case, model="Private van")
        self.login("owner")
        form = evidence_form("vehicle-location")(
            self.payload("vehicle-location", vehicle=str(other.pk)),
            kind="vehicle-location",
            instance=VehicleLocationEvidence(
                case=self.case, uploaded_by=self.users["owner"]
            ),
        )
        self.assertFalse(form.is_valid())
        item = VehicleLocationEvidence(
            case=self.case,
            uploaded_by=self.users["owner"],
            vehicle=other,
            title="Manual observation",
            address="Synthetic place",
        )
        with self.assertRaises(ValidationError):
            item.save()
        batch = self.new_batch()
        item.vehicle = self.vehicle
        item.import_batch = batch
        item.case = self.other_case
        item.vehicle = other
        item.source_row = 2
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            VehicleLocationEvidence.objects.bulk_create(
                [
                    VehicleLocationEvidence(
                        case=self.case,
                        uploaded_by=self.users["owner"],
                        vehicle=self.vehicle,
                        title="Invalid pairing",
                        address="Synthetic place",
                        sha256="0" * 64,
                        import_batch=batch,
                        source_row=None,
                    )
                ]
            )


class ConcurrentVehicleImportTests(TransactionTestCase):
    def test_parallel_upload_and_completion_retries_retain_one_batch_and_one_row(self):
        owner = get_user_model().objects.create_user(
            username="parallel-vehicle-owner", email="parallel-vehicle@example.test"
        )
        EmailAddress.objects.create(
            user=owner, email=owner.email, verified=True, primary=True
        )
        case = Case.objects.create(owner=owner)
        vehicle = Vehicle.objects.create(case=case, model="Synthetic van")
        submission = str(uuid4())
        upload_url = reverse(
            "evidence:vehicle_import_upload", kwargs={"case_id": case.pk}
        )

        def parallel_post(url, data):
            ready = Barrier(2)

            def submit(_):
                close_old_connections()
                try:
                    client = Client()
                    client.force_login(owner)
                    ready.wait(timeout=10)
                    response = client.post(url, data())
                    return response.status_code, response.get("Location")
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(submit, range(2)))

        with TemporaryDirectory() as directory, self.settings(MEDIA_ROOT=directory):
            uploaded = parallel_post(
                upload_url,
                lambda: {
                    "submission_id": submission,
                    "timezone": "UTC",
                    "use_created_time": "on",
                    "file": SimpleUploadedFile(
                        "synthetic.csv", csv_content(), "text/csv"
                    ),
                },
            )
            self.assertEqual([status for status, _ in uploaded], [302, 302])
            self.assertEqual(len({url for _, url in uploaded}), 1)
            self.assertEqual(VehicleLocationImport.objects.count(), 1)
            self.assertEqual(DocumentEvidence.objects.count(), 1)
            completed = parallel_post(
                uploaded[0][1], lambda: {"vehicle_0": str(vehicle.pk), "confirm": "on"}
            )
            self.assertEqual([status for status, _ in completed], [302, 302])
            self.assertEqual(VehicleLocationEvidence.objects.count(), 1)
            self.assertIsNotNone(VehicleLocationImport.objects.get().completed_at)

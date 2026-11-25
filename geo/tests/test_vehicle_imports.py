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
from django_tasks_db.models import DBTaskResult

from cases.models import Case
from core.analysis.bulk import prepare_case_analysis_batch
from core.evidence.form_registry import evidence_form
from core.models import CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence, VehicleLocationImport
from geo.testing import csv_content, parse_csv
from geo.vehicle_import_jobs import enqueue_completion, process_import
from geo.vehicle_imports import iter_csv, iter_import
from vehicle.models import Vehicle


class VehicleCSVParserTests(SimpleTestCase):
    def test_missing_speed_placeholder_preserves_raw_value_and_differs_from_zero(self):
        for speed in ("-", " - ", "", "0 km/h", "12.5 km/h"):
            with self.subTest(speed=speed):
                row = parse_csv(csv_content(**{"Speed": speed}), "UTC")[0]
                self.assertFalse(row.errors)
                self.assertEqual(row.raw["Speed"], speed)
                self.assertEqual(
                    row.values["speed_kmh"],
                    {"0 km/h": 0, "12.5 km/h": 12.5}.get(speed),
                )

    def test_v1_keeps_its_original_speed_interpretation(self):
        for speed in ("-", "", "0 km/h"):
            with self.subTest(speed=speed):
                row = next(
                    iter_csv(
                        io.BytesIO(csv_content(**{"Speed": speed})),
                        "UTC",
                        parser_version="asset-location.v1",
                    )
                )
                self.assertEqual(bool(row.errors), speed == "-")
                self.assertEqual(row.raw["Speed"], speed)

    def test_older_versions_keep_the_original_timestamp_format(self):
        for version in ("asset-location.v1", "asset-location.v2"):
            with self.subTest(version=version):
                values = {
                    "Created Time": "2026-09-16 14:57:22",
                    "Speed": "-" if version == "asset-location.v2" else "",
                }
                row = next(
                    iter_csv(
                        io.BytesIO(csv_content(**values)),
                        "UTC",
                        parser_version=version,
                    )
                )
                self.assertEqual(len(row.errors), 1)
                self.assertTrue(row.errors[0].startswith("Created Time: "))
                self.assertEqual(
                    row.values["received_at"],
                    datetime(2026, 9, 16, 14, 57, 25, tzinfo=UTC),
                )

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

    def test_timestamp_columns_can_use_different_formats(self):
        row = parse_csv(
            csv_content(**{"Received Time": "2026-09-16 14:57:25"}), "America/Toronto"
        )[0]
        self.assertFalse(row.errors)
        self.assertEqual(
            row.values["observed_at"], datetime(2026, 9, 16, 18, 57, 22, tzinfo=UTC)
        )
        self.assertEqual(
            row.values["received_at"], datetime(2026, 9, 16, 18, 57, 25, tzinfo=UTC)
        )

    def test_24_hour_timestamps_reject_ambiguous_and_nonexistent_local_times(self):
        for timestamp in ("2026-11-01 01:30:00", "2026-03-08 02:30:00"):
            with self.subTest(timestamp=timestamp):
                row = parse_csv(
                    csv_content(**{"Created Time": timestamp}), "America/Toronto"
                )[0]
                self.assertEqual(len(row.errors), 1)
                self.assertIn(
                    "Created Time: This local time is ambiguous", row.errors[0]
                )

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
        with patch("geo.vehicle_imports.MAX_ROWS", 2):
            with self.assertRaises(ValidationError):
                parse_csv(csv_content(row, row, row), "UTC")
            self.assertEqual(len(parse_csv(csv_content(row, row), "UTC")), 2)

    def test_invalid_fields_and_dst_times_require_correction(self):
        for values in (
            {"Address": " \n"},
            {"Asset": "", "Serial Number": ""},
            {"Speed": "-1 km/h"},
            {"Speed": "- km/h"},
            {"Speed": "--"},
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
            "geo:vehicle_import_upload", kwargs={"case_id": (case or self.case).pk}
        )

    def review_url(self, batch, case=None):
        return reverse(
            "geo:vehicle_import_review",
            kwargs={"case_id": (case or self.case).pk, "import_id": batch.pk},
        )

    def upload(self, content=None, *, case=None, **changes):
        return self.client.post(
            self.upload_url(case),
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

    def run_job(self, batch):
        batch.refresh_from_db()
        result = process_import(str(batch.pk), str(batch.job_token))
        batch.refresh_from_db()
        return result

    def new_batch(self, content=None, **changes):
        self.login("editor")
        response = self.upload(content, **changes)
        self.assertEqual(response.status_code, 302, response.content)
        batch = VehicleLocationImport.objects.latest("created_at")
        self.assertEqual(batch.status, "queued")
        self.assertFalse(batch.rows.exists())
        self.run_job(batch)
        self.assertEqual(batch.status, "review")
        return batch

    def complete(self, batch, *, process=True, **changes):
        response = self.client.post(
            self.review_url(batch),
            {
                "vehicle_0": str(self.vehicle.pk),
                "confirm": "on",
            }
            | changes,
        )
        if process and response.status_code == 302:
            self.run_job(batch)
        return response

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
        self.assertEqual(self.complete(batch, process=False).status_code, 302)
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        self.run_job(batch)
        token = str(batch.job_token)
        self.assertEqual(process_import(str(batch.pk), token), "complete")
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

    def test_import_retains_24_hour_timestamp_cells_and_converts_to_utc(self):
        timestamps = {
            "Created Time": "2026-01-20 05:16:09",
            "Received Time": "2026-01-20 05:15:47",
        }
        original = csv_content(**timestamps)
        batch = self.new_batch(original)
        self.assertEqual(batch.parser_version, "asset-location.v3")
        self.assertEqual(batch.invalid_count, 0)
        self.assertEqual(batch.document.file.read(), original)
        self.assertContains(
            self.client.get(self.review_url(batch)), "20 Jan 2026, 10:16:09"
        )

        self.assertEqual(self.complete(batch).status_code, 302)
        self.assertEqual(batch.status, "complete")
        item = batch.observations.get()
        self.assertEqual(item.observed_at, datetime(2026, 1, 20, 10, 16, 9, tzinfo=UTC))
        self.assertEqual(
            item.received_at, datetime(2026, 1, 20, 10, 15, 47, tzinfo=UTC)
        )
        retained = json.loads(item.original_bytes())
        self.assertEqual(retained["import"]["parser_version"], "asset-location.v3")
        for column, timestamp in timestamps.items():
            self.assertEqual(retained["raw_row"][column], timestamp)

    def test_missing_speeds_import_without_changing_originals_or_zero(self):
        raw = parse_csv(csv_content(), "UTC")[0].raw
        original = csv_content(
            raw | {"Speed": "-"}, raw | {"Speed": ""}, raw | {"Speed": "0 km/h"}
        )
        batch = self.new_batch(original)
        self.assertEqual(batch.parser_version, "asset-location.v3")
        self.assertEqual(batch.invalid_count, 0)
        self.assertEqual(batch.row_count, 3)
        self.assertEqual(
            list(batch.rows.values_list("speed_kmh", flat=True)), [None, None, 0]
        )
        self.assertEqual(self.complete(batch).status_code, 302)
        self.assertEqual(batch.status, "complete")
        observations = list(batch.observations.order_by("source_row"))
        self.assertEqual(len(observations), 3)
        for item, speed, value in zip(
            observations, ("-", "", "0 km/h"), (None, None, "0.000"), strict=True
        ):
            original_json = json.loads(item.original_bytes())
            self.assertEqual(original_json["raw_row"]["Speed"], speed)
            self.assertEqual(original_json["speed_kmh"], value)
            self.assertEqual(
                original_json["import"]["parser_version"], "asset-location.v3"
            )
            self.assertEqual(
                hashlib.sha256(item.original_bytes()).hexdigest(), item.sha256
            )
        self.assertEqual(batch.document.file.read(), original)
        self.assertEqual(batch.document.sha256, hashlib.sha256(original).hexdigest())

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
        batch = self.new_batch(
            csv_content(*([first] * 60), first | {"Speed": "invalid"})
        )
        response = self.client.get(self.review_url(batch))
        self.assertContains(response, "Rows to correct")
        self.assertEqual([row.number for row in response.context["rows"]], [62])
        self.assertContains(response, "View all 61 source rows")
        response = self.client.get(self.review_url(batch), {"rows": "all"})
        self.assertEqual(response.context["rows"][0].number, 2)
        self.assertContains(response, "?rows=all&amp;page=2")
        self.assertEqual(self.complete(batch).status_code, 400)
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        self.assertTrue(DocumentEvidence.objects.filter(pk=batch.document_id).exists())
        with patch.object(
            VehicleLocationEvidence,
            "_prepare_original",
            side_effect=ValidationError("synthetic failure"),
        ):
            valid = self.new_batch()
            self.assertEqual(self.complete(valid).status_code, 302)
        valid.refresh_from_db()
        self.assertIsNone(valid.completed_at)
        self.assertEqual(valid.status, "failed")

    def test_atomic_rollback_after_a_later_row_fails(self):
        first = parse_csv(csv_content(), "UTC")[0].raw
        batch = self.new_batch(csv_content(first, first, first | {"Heading": "E"}))
        self.assertEqual(batch.duplicate_count, 1)
        create = VehicleLocationEvidence.objects.bulk_create
        calls = []

        def fail_second(items, **kwargs):
            calls.append(items)
            if len(calls) == 2:
                raise IntegrityError("synthetic failure")
            return create(items, **kwargs)

        with (
            patch("geo.vehicle_import_jobs.CHUNK_SIZE", 1),
            patch.object(VehicleLocationEvidence.objects, "bulk_create", fail_second),
        ):
            self.assertEqual(self.complete(batch).status_code, 302)
        self.assertEqual(len(calls), 2)
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        batch.refresh_from_db()
        self.assertIsNone(batch.completed_at)
        self.assertEqual(batch.status, "failed")
        self.assertEqual(
            self.client.post(self.review_url(batch), {"action": "retry"}).status_code,
            302,
        )
        self.run_job(batch)
        self.assertEqual(VehicleLocationEvidence.objects.count(), 2)

    def test_possible_duplicates_can_be_kept_within_and_between_exports(self):
        row = parse_csv(csv_content(), "UTC")[0].raw
        batch = self.new_batch(csv_content(row, row))
        self.assertContains(
            self.client.get(self.review_url(batch)), "Possible duplicate"
        )
        self.assertEqual(self.complete(batch, confirm_duplicates="on").status_code, 302)
        self.assertEqual(batch.observations.count(), 2)
        another = self.new_batch()
        self.assertContains(
            self.client.get(self.review_url(another)), "Possible duplicate"
        )
        self.assertEqual(
            self.complete(another, confirm_duplicates="on").status_code, 302
        )
        self.assertEqual(VehicleLocationEvidence.objects.count(), 3)

    def test_unchecked_duplicates_skip_matches_and_retain_the_complete_source(self):
        row = parse_csv(csv_content(), "UTC")[0].raw
        original = csv_content(row, row, row | {"Heading": "E"}, row)
        with patch("geo.vehicle_import_jobs.CHUNK_SIZE", 1):
            batch = self.new_batch(original)
            review = self.client.get(self.review_url(batch))
            self.assertContains(review, "2 observations ready without them")
            self.assertContains(
                review, "Created Time is the reported observation time."
            )
            self.assertFalse(
                review.context["form"].fields["confirm_duplicates"].required
            )
            self.assertEqual(self.complete(batch).status_code, 302)
        self.assertEqual(batch.status, "complete")
        self.assertFalse(batch.duplicates_confirmed)
        self.assertEqual(
            list(
                batch.observations.order_by("source_row").values_list(
                    "source_row", flat=True
                )
            ),
            [2, 4],
        )
        self.assertEqual(batch.document.file.read(), original)
        self.assertEqual(batch.document.sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(batch.rows.count(), 4)
        results = self.client.get(self.review_url(batch))
        self.assertContains(results, "2 observations imported.")
        self.assertContains(results, "2 possible duplicate rows skipped.")
        self.assertContains(results, reverse("geo:case_map", args=(self.case.pk,)))
        self.assertContains(results, "Open case map")
        self.assertContains(results, "View source CSV")
        self.assertNotContains(results, "Refresh status")
        self.assertEqual(self.complete(batch, confirm_duplicates="on").status_code, 302)
        self.assertEqual(batch.observations.count(), 2)
        for field, value in (("duplicates_confirmed", True), ("duplicate_count", 0)):
            batch.refresh_from_db()
            setattr(batch, field, value)
            with self.assertRaises(ValidationError):
                batch.save()

    def test_all_duplicates_complete_with_no_new_observations_and_can_be_retried(self):
        first = self.new_batch()
        self.assertEqual(self.complete(first).status_code, 302)
        VehicleLocationEvidence.objects.filter(import_batch=first).update(
            is_archived=True,
            archive_changed_at=datetime(2026, 10, 1, tzinfo=UTC),
            archive_changed_by=self.users["editor"],
        )
        another = self.new_batch()
        self.assertEqual(another.duplicate_count, 1)
        self.assertEqual(self.complete(another).status_code, 302)
        self.assertEqual(another.status, "complete")
        self.assertIsNotNone(another.completed_at)
        self.assertFalse(another.observations.exists())
        self.assertEqual(another.rows.count(), 1)
        result = self.client.get(self.review_url(another))
        self.assertContains(result, "0 observations imported.")
        self.assertContains(result, "1 possible duplicate row skipped.")
        self.assertNotContains(result, "Open case map")
        self.assertContains(result, "View source CSV")
        self.assertEqual(self.complete(another).status_code, 302)
        self.assertEqual(VehicleLocationEvidence.objects.count(), 1)

    def test_large_import_uses_background_jobs_and_paginated_review_and_results(self):
        row = parse_csv(csv_content(), "UTC")[0].raw
        row["Address"] = "Synthetic address " + "a" * 600
        content = csv_content(*([row] * 2000))
        self.assertGreater(len(content), 1024 * 1024)
        batch = self.new_batch(content)
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        review = self.client.get(self.review_url(batch))
        self.assertEqual(review.context["batch"].row_count, 2000)
        self.assertEqual(len(review.context["rows"]), 50)
        self.assertEqual(review.context["page_obj"].paginator.num_pages, 40)
        last = self.client.get(self.review_url(batch), {"page": 40})
        self.assertEqual(last.context["rows"][0].number, 1952)
        self.assertEqual(self.complete(batch, confirm_duplicates="on").status_code, 302)
        self.assertEqual(batch.observations.count(), 2000)
        batch.refresh_from_db()
        self.assertIsNotNone(batch.completed_at)
        results = self.client.get(self.review_url(batch))
        self.assertEqual(len(results.context["observations"]), 50)
        self.assertEqual(results.context["page_obj"].paginator.count, 2000)
        self.assertContains(results, "Follow import analysis")
        analysis = CaseAnalysisBatch.objects.get(
            scope__type="vehicle", scope__id=str(batch.pk)
        )
        self.assertFalse(analysis.runs.exists())
        for _ in range(2):
            self.assertEqual(prepare_case_analysis_batch(analysis.pk), "queued")
        analysis.refresh_from_db()
        self.assertEqual(analysis.processed_sources, 50)
        runs = analysis.runs.filter(operation="location_quality").order_by("created_at")
        eligible = set(
            str(pk)
            for pk in batch.observations.order_by(
                "vehicle_id", "observed_at", "pk"
            ).values_list("pk", flat=True)[:50]
        )
        self.assertEqual(
            {item["evidence_id"] for run in runs for item in run.inputs},
            eligible,
        )
        self.assertEqual(self.complete(batch, confirm_duplicates="on").status_code, 302)
        self.assertEqual(
            CaseAnalysisBatch.objects.filter(scope__type="vehicle").count(), 1
        )

    def test_upload_retry_preserves_file_and_timezone_within_uploader_scope(self):
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
        owner_upload = self.upload(submission_id=submission)
        self.assertEqual(owner_upload.status_code, 302)
        self.assertEqual(self.upload(submission_id=submission).url, owner_upload.url)
        self.login("outsider")
        other_upload = self.upload(case=self.other_case, submission_id=submission)
        self.assertEqual(other_upload.status_code, 302)
        self.assertNotEqual(owner_upload.url, other_upload.url)
        self.assertEqual(VehicleLocationImport.objects.count(), 3)
        self.assertEqual(DocumentEvidence.objects.count(), 3)

    def test_upload_vehicle_is_case_scoped_and_initializes_review_mappings(self):
        other_vehicle = Vehicle.objects.create(
            case=self.other_case, model="Private van"
        )
        self.login("editor")
        page = self.client.get(self.upload_url())
        self.assertEqual(page.context["form"]["timezone"].value(), "America/Toronto")
        self.assertNotContains(page, "Private van")
        page = self.client.get(self.upload_url(), {"vehicle": self.vehicle.pk})
        self.assertEqual(page.context["form"]["vehicle"].value(), self.vehicle.pk)
        vehicle_url = reverse("vehicle:detail", args=[self.case.pk, self.vehicle.pk])
        self.assertContains(page, f'href="{vehicle_url}">Back to vehicle')
        self.assertContains(page, f'href="{vehicle_url}">Cancel')
        page = self.client.post(f"{self.upload_url()}?vehicle={self.vehicle.pk}", {})
        self.assertContains(page, f'href="{vehicle_url}">Cancel', status_code=400)
        for invalid in (other_vehicle.pk, "invalid"):
            page = self.client.get(self.upload_url(), {"vehicle": invalid})
            self.assertIsNone(page.context["form"]["vehicle"].value())
            self.assertContains(page, "Back to evidence")
        self.assertEqual(self.upload(vehicle=str(other_vehicle.pk)).status_code, 400)
        self.assertFalse(DocumentEvidence.objects.exists())
        submission = str(uuid4())
        batch = self.new_batch(vehicle=str(self.vehicle.pk), submission_id=submission)
        self.assertEqual(batch.selected_vehicle, self.vehicle)
        review = self.client.get(self.review_url(batch))
        self.assertEqual(
            str(review.context["form"]["vehicle_0"].value()), str(self.vehicle.pk)
        )
        self.assertEqual(
            self.upload(
                submission_id=submission, vehicle=str(self.vehicle.pk)
            ).status_code,
            302,
        )
        self.assertEqual(
            self.upload(submission_id=submission, vehicle="").status_code, 409
        )
        self.assertEqual(VehicleLocationImport.objects.count(), 1)
        self.assertEqual(self.complete(batch).status_code, 302)

    def test_add_vehicle_from_review_returns_without_publishing_or_weaker_access(self):
        row = parse_csv(csv_content(), "UTC")[0].raw
        batch = self.new_batch(
            csv_content(row, row, row | {"Asset": "Second asset"}),
            vehicle=str(self.vehicle.pk),
        )
        alternate = Vehicle.objects.create(case=self.case, make="Alternate vehicle")
        review_url = self.review_url(batch)
        create_url = reverse("vehicle:create_for_import", args=[self.case.pk, batch.pk])
        return_url = f"{review_url}#mapping-heading"
        self.assertContains(self.client.get(review_url), "Add a vehicle")
        draft = {
            "action": "add_vehicle",
            "vehicle_0": str(alternate.pk),
            "vehicle_1": "",
            "confirm_duplicates": "on",
            "confirm": "on",
        }
        self.assertRedirects(self.client.post(review_url, draft), create_url)
        response = self.client.get(create_url)
        self.assertContains(response, "Add vehicle and return")
        self.assertContains(response, f'href="{return_url}">Cancel')
        response = self.client.post(create_url, {"color": "Blue"})
        self.assertContains(response, f'href="{return_url}"', status_code=400)
        cancelled = self.client.get(return_url).context["form"]
        self.assertEqual(cancelled["vehicle_0"].value(), str(alternate.pk))
        self.assertEqual(cancelled["vehicle_1"].value(), "")
        self.assertTrue(cancelled["confirm_duplicates"].value())
        self.assertFalse(cancelled["confirm"].value())

        for role, expected in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(self.client.post(review_url, draft).status_code, expected)
            self.assertEqual(self.client.get(create_url).status_code, expected)
            self.assertEqual(
                self.client.post(create_url, {"make": "Unauthorized"}).status_code,
                expected,
            )
        self.login("editor")
        mismatched_url = reverse(
            "vehicle:create_for_import", args=[self.case.pk, uuid4()]
        )
        self.assertEqual(self.client.get(mismatched_url).status_code, 404)
        self.assertEqual(
            self.client.post(mismatched_url, {"make": "Unauthorized"}).status_code, 404
        )

        self.assertRedirects(self.client.post(review_url, draft), create_url)
        response = self.client.post(
            create_url, {"make": "Synthetic second vehicle"}, follow=True
        )
        self.assertRedirects(response, return_url)
        review = response
        self.assertContains(review, "Synthetic second vehicle")
        mapping = review.context["form"]
        self.assertEqual(mapping["vehicle_0"].value(), str(alternate.pk))
        self.assertEqual(mapping["vehicle_1"].value(), "")
        self.assertTrue(mapping["confirm_duplicates"].value())
        self.assertFalse(mapping["confirm"].value())
        batch.refresh_from_db()
        self.assertEqual(batch.status, "review")
        self.assertFalse(batch.vehicle_mapping)
        self.assertFalse(batch.observations.exists())
        self.assertEqual(
            self.complete(
                batch,
                vehicle_0=str(alternate.pk),
                vehicle_1=str(self.vehicle.pk),
                confirm_duplicates="on",
            ).status_code,
            302,
        )
        response = self.client.post(create_url, {"make": "Already finished"})
        self.assertRedirects(response, review_url)
        self.assertFalse(Vehicle.objects.filter(make="Already finished").exists())
        self.assertEqual(batch.observations.filter(vehicle=alternate).count(), 2)
        self.assertEqual(batch.observations.filter(vehicle=self.vehicle).count(), 1)

    def test_retained_import_blocks_vehicle_deletion_before_publication(self):
        batch = self.new_batch(vehicle=str(self.vehicle.pk))
        self.login("owner")
        delete_url = reverse("vehicle:delete", args=(self.case.pk, self.vehicle.pk))
        page = self.client.get(delete_url)
        self.assertContains(page, f'href="{self.review_url(batch)}"')
        self.assertContains(page, "synthetic-assets.csv")
        self.assertNotContains(page, "Permanently delete vehicle")
        blocked = self.client.post(delete_url, {"confirm": "on"})
        self.assertContains(
            blocked, "linked to a retained location import", status_code=400
        )
        self.assertTrue(Vehicle.objects.filter(pk=self.vehicle.pk).exists())

    def test_worker_rechecks_access_and_retry_ignores_replaced_task_token(self):
        self.login("editor")
        self.assertEqual(self.upload().status_code, 302)
        batch = VehicleLocationImport.objects.get()
        old_token = str(batch.job_token)
        self.case.memberships.filter(user=self.users["editor"]).update(role="viewer")
        self.run_job(batch)
        self.assertEqual(batch.status, "failed")
        self.assertFalse(batch.rows.exists())
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        self.assertEqual(
            self.client.post(self.review_url(batch), {"action": "retry"}).status_code,
            403,
        )
        self.login("owner")
        self.assertEqual(
            self.client.post(self.review_url(batch), {"action": "retry"}).status_code,
            302,
        )
        batch.refresh_from_db()
        replacement = str(batch.job_token)
        self.assertNotEqual(old_token, replacement)
        process_import(str(batch.pk), old_token)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "queued")
        self.run_job(batch)
        self.assertEqual(batch.status, "review")
        self.assertEqual(batch.rows.count(), 1)
        self.assertEqual(self.complete(batch, process=False).status_code, 302)
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        self.run_job(batch)
        item = batch.observations.get()
        self.assertEqual(item.uploaded_by, self.users["owner"])
        self.assertEqual(batch.uploaded_by, self.users["editor"])
        process_import(str(batch.pk), str(batch.job_token))
        self.assertEqual(batch.observations.count(), 1)

    def test_archived_vehicle_csv_cannot_confirm_or_publish_observations(self):
        batch = self.new_batch()
        archive_url = self.url("archive", batch.document, kind="document")
        processing = reverse("core:analysis:processing", args=[self.case.pk])
        self.assertContains(
            self.client.get(processing), "Vehicle imports to prepare or review"
        )
        self.client.post(archive_url)
        history = self.client.get(processing)
        self.assertNotContains(history, "Vehicle imports to prepare or review")
        self.assertContains(history, "Restore the source before continuing this import")
        create_vehicle = reverse(
            "vehicle:create_for_import", args=[self.case.pk, batch.pk]
        )
        self.assertRedirects(self.client.get(create_vehicle), self.review_url(batch))
        self.assertRedirects(
            self.client.post(create_vehicle, {"make": "Blocked vehicle"}),
            self.review_url(batch),
        )
        self.assertFalse(
            Vehicle.objects.filter(case=self.case, make="Blocked vehicle").exists()
        )
        self.assertNotContains(
            self.client.get(self.review_url(batch)), "Import locations"
        )
        self.assertContains(
            self.complete(batch, process=False),
            "Restore this CSV before importing vehicle locations.",
            status_code=400,
        )
        batch.refresh_from_db()
        self.assertEqual(batch.status, "review")
        self.client.post(self.url("restore", batch.document, kind="document"))
        self.assertEqual(self.complete(batch, process=False).status_code, 302)

        def archive_after_reading(source):
            yield from iter_import(source)
            self.assertEqual(self.client.post(archive_url).status_code, 302)

        with patch(
            "geo.vehicle_import_jobs.iter_import", side_effect=archive_after_reading
        ):
            self.assertEqual(self.run_job(batch), "failed")
        self.assertEqual(batch.error_code, "source_archived")
        self.assertIsNone(batch.completed_at)
        self.assertFalse(batch.observations.exists())

    def test_revoked_confirmation_actor_cannot_publish_queued_observations(self):
        batch = self.new_batch()
        self.assertEqual(self.complete(batch, process=False).status_code, 302)
        self.case.memberships.filter(user=self.users["editor"]).delete()
        self.run_job(batch)
        self.assertEqual(batch.status, "failed")
        self.assertIsNone(batch.completed_at)
        self.assertFalse(VehicleLocationEvidence.objects.exists())

    def test_failed_import_retries_are_bounded_and_running_recovery_is_explicit(self):
        self.login("editor")
        self.assertEqual(self.upload(b"invalid").status_code, 302)
        batch = VehicleLocationImport.objects.get()
        self.run_job(batch)
        for attempt in (2, 3):
            self.assertEqual(
                self.client.post(
                    self.review_url(batch), {"action": "retry"}
                ).status_code,
                302,
            )
            self.run_job(batch)
            self.assertEqual(batch.attempts, attempt)
            self.assertEqual(batch.status, "failed")
        self.assertEqual(
            self.client.post(self.review_url(batch), {"action": "retry"}).status_code,
            400,
        )
        self.assertFalse(VehicleLocationEvidence.objects.exists())

        self.assertEqual(self.upload().status_code, 302)
        running = VehicleLocationImport.objects.latest("created_at")
        old_token = str(running.job_token)
        VehicleLocationImport.objects.filter(pk=running.pk).update(status="running")
        self.assertEqual(
            self.client.post(self.review_url(running), {"action": "retry"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                self.review_url(running),
                {"action": "retry", "worker_stopped": "on"},
            ).status_code,
            302,
        )
        process_import(str(running.pk), old_token)
        self.run_job(running)
        self.assertEqual(running.status, "review")
        self.assertEqual(running.rows.count(), 1)

    def test_legacy_pending_review_is_prepared_only_by_an_editor_post(self):
        self.login("editor")
        self.assertEqual(self.upload().status_code, 302)
        batch = VehicleLocationImport.objects.get()
        VehicleLocationImport.objects.filter(pk=batch.pk).update(status="pending")
        self.login("viewer")
        self.assertEqual(self.client.get(self.review_url(batch)).status_code, 200)
        self.assertEqual(
            self.client.post(self.review_url(batch), {"action": "prepare"}).status_code,
            403,
        )
        self.login("editor")
        self.assertContains(self.client.get(self.review_url(batch)), "Prepare import")
        batch.refresh_from_db()
        self.assertEqual(batch.status, "pending")
        self.assertEqual(
            self.client.post(self.review_url(batch), {"action": "prepare"}).status_code,
            302,
        )
        self.run_job(batch)
        self.assertEqual(batch.status, "review")

    def test_queue_payloads_contain_only_uuids_and_rollback_with_the_batch(self):
        batch = self.new_batch()
        queued = DBTaskResult.objects.get(task_path="geo.tasks.process_vehicle_import")
        self.assertEqual(
            queued.args_kwargs,
            {"args": [str(batch.pk), str(batch.job_token)], "kwargs": {}},
        )
        original_token = batch.job_token
        with self.assertRaisesMessage(ValueError, "rollback"), transaction.atomic():
            pending = enqueue_completion(
                self.users["editor"],
                self.case.pk,
                batch.pk,
                {"0": str(self.vehicle.pk)},
            )
            self.assertEqual(pending.status, "queued")
            self.assertEqual(DBTaskResult.objects.count(), 2)
            raise ValueError("rollback")
        batch.refresh_from_db()
        self.assertEqual(batch.status, "review")
        self.assertEqual(batch.phase, "prepare")
        self.assertEqual(batch.job_token, original_token)
        self.assertEqual(batch.vehicle_mapping, {})
        self.assertEqual(DBTaskResult.objects.count(), 1)
        self.assertFalse(batch.observations.exists())

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
        self.assertEqual(self.upload(b"").status_code, 400)
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
        self.assertEqual(self.complete(batch).status_code, 302)
        self.assertEqual(batch.status, "failed")
        self.assertContains(
            self.client.get(self.review_url(batch)), "No partial import is published"
        )
        self.assertFalse(VehicleLocationEvidence.objects.exists())

    def test_malformed_upload_is_retained_and_fails_safely_in_worker(self):
        self.login("editor")
        self.assertEqual(self.upload(b"invalid").status_code, 302)
        batch = VehicleLocationImport.objects.get()
        self.assertEqual(batch.document.file.read(), b"invalid")
        self.run_job(batch)
        self.assertEqual(batch.status, "failed")
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        self.assertNotContains(self.client.get(self.review_url(batch)), "Traceback")

    def test_source_integrity_and_parser_version_are_checked_before_completion(self):
        batch = self.new_batch()
        with batch.document.file.storage.open(
            batch.document.file.name, "wb"
        ) as original:
            original.write(csv_content(**{"Heading": "E"}))
        self.assertEqual(self.complete(batch).status_code, 302)
        self.assertEqual(batch.status, "failed")
        self.assertFalse(VehicleLocationEvidence.objects.exists())
        another = self.new_batch()
        VehicleLocationImport.objects.filter(pk=another.pk).update(
            parser_version="unsupported"
        )
        self.assertEqual(self.complete(another).status_code, 302)
        self.assertEqual(another.status, "failed")
        self.assertFalse(VehicleLocationEvidence.objects.exists())

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

        with patch("core.storage.transaction.atomic", side_effect=atomic_context):
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
            "vehicle:detail",
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
            "vehicle:delete",
            kwargs={"case_id": self.case.pk, "vehicle_id": self.vehicle.pk},
        )
        blocked = self.client.get(delete_url)
        self.assertContains(blocked, self.url("detail", item, "vehicle-location"))
        self.assertContains(blocked, "View all linked locations")
        self.assertNotContains(blocked, "Permanently delete vehicle")
        self.assertContains(
            self.client.post(delete_url, {"confirm": "on"}),
            "linked to retained location evidence",
            status_code=400,
        )
        self.client.post(self.url("archive", item, kind="vehicle-location"))
        self.assertContains(self.client.get(delete_url), "Archived")
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
        upload_url = reverse("geo:vehicle_import_upload", kwargs={"case_id": case.pk})

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
            batch = VehicleLocationImport.objects.get()
            process_import(str(batch.pk), str(batch.job_token))
            self.assertFalse(VehicleLocationEvidence.objects.exists())
            completed = parallel_post(
                uploaded[0][1], lambda: {"vehicle_0": str(vehicle.pk), "confirm": "on"}
            )
            self.assertEqual([status for status, _ in completed], [302, 302])
            self.assertFalse(VehicleLocationEvidence.objects.exists())
            batch.refresh_from_db()
            token = str(batch.job_token)

            def process(_):
                close_old_connections()
                try:
                    return process_import(str(batch.pk), token)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(process, range(2)))
            self.assertEqual(VehicleLocationEvidence.objects.count(), 1)
            self.assertIsNotNone(VehicleLocationImport.objects.get().completed_at)

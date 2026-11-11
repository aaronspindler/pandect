import io
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence, VehicleLocationImport
from geo.testing import csv_content
from geo.vehicle_import_jobs import (
    enqueue_completion,
    enqueue_preparation,
    process_import,
)
from geo.vehicle_imports import HEADERS, iter_csv, parse_csv


class StreamingVehicleCSVTests(SimpleTestCase):
    def test_stream_reads_are_bounded_and_multiline_record_allocation_is_bounded(self):
        class TrackedFile(io.BytesIO):
            def read(self, size=-1):
                self.assert_bounded(size)
                return super().read(size)

            @staticmethod
            def assert_bounded(size):
                if not 0 < size <= 64 * 1024:
                    raise AssertionError(f"Unbounded source read: {size}")

        self.assertEqual(len(list(iter_csv(TrackedFile(csv_content()), "UTC"))), 1)
        # Many tiny quoted fields across short physical lines must not let the
        # csv parser allocate an entire 100 MiB logical record before validation.
        hostile = ",".join(HEADERS).encode() + b"\n" + b'"x\n",' * 18_000 + b'"last"\n'
        with self.assertRaisesMessage(ValidationError, "oversized record"):
            parse_csv(hostile, "UTC")


class VehicleImportJobBoundaryTests(EvidenceViewTestMixin, TestCase):
    def batch(self, content=None, *, prepare=True):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Synthetic CSV",
            file=SimpleUploadedFile(
                "synthetic.csv", content or csv_content(), "text/csv"
            ),
        )
        batch = VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            document=document,
            timezone="UTC",
        )
        enqueue_preparation(self.users["editor"], self.case.pk, batch.pk)
        batch.refresh_from_db()
        if prepare:
            self.assertEqual(self.run_job(batch), "review")
        return batch

    def run_job(self, batch):
        result = process_import(str(batch.pk), str(batch.job_token))
        batch.refresh_from_db()
        return result

    def queue_completion(self, batch, *, duplicates=False):
        enqueue_completion(
            self.users["editor"],
            self.case.pk,
            batch.pk,
            {str(index): str(self.vehicle.pk) for index in range(len(batch.groups))},
            confirm_duplicates=duplicates,
        )
        batch.refresh_from_db()

    def test_new_duplicates_after_preview_are_skipped_before_publication(self):
        first, second = self.batch(), self.batch()
        self.assertEqual(second.duplicate_count, 0)
        self.queue_completion(first)
        self.assertEqual(self.run_job(first), "complete")
        self.queue_completion(second)
        self.assertEqual(self.run_job(second), "complete")
        self.assertEqual(second.duplicate_count, 1)
        self.assertTrue(second.rows.get().duplicate)
        self.assertFalse(second.observations.exists())
        self.assertEqual(VehicleLocationEvidence.objects.count(), 1)

    def test_deleted_matches_stop_suppressing_the_first_row_across_chunks(self):
        first = self.batch()
        self.queue_completion(first)
        self.assertEqual(self.run_job(first), "complete")
        row = parse_csv(csv_content(), "UTC")[0].raw
        with patch("geo.vehicle_import_jobs.CHUNK_SIZE", 1):
            second = self.batch(csv_content(row, row))
            self.assertEqual(second.duplicate_count, 2)
            self.queue_completion(second)
            first.observations.all().delete()
            self.assertEqual(self.run_job(second), "complete")
        self.assertEqual(second.duplicate_count, 1)
        self.assertEqual(second.observations.get().source_row, 2)
        self.assertEqual(
            list(second.rows.values_list("duplicate", flat=True)), [False, True]
        )

    def test_v1_jobs_retain_their_original_rules_and_can_still_complete(self):
        for speed in ("-", "0 km/h"):
            with self.subTest(speed=speed):
                batch = self.batch(csv_content(**{"Speed": speed}), prepare=False)
                VehicleLocationImport.objects.filter(pk=batch.pk).update(
                    parser_version="asset-location.v1"
                )
                self.assertEqual(self.run_job(batch), "review")
                self.assertEqual(batch.invalid_count, int(speed == "-"))
                if speed == "-":
                    with self.assertRaises(ValidationError):
                        self.queue_completion(batch)
                    self.assertFalse(batch.observations.exists())
                else:
                    self.queue_completion(batch)
                    self.assertEqual(self.run_job(batch), "complete")
                    item = batch.observations.get()
                    self.assertEqual(item.speed_kmh, 0)
                    self.assertEqual(
                        item.original_data()["import"]["parser_version"],
                        "asset-location.v1",
                    )

    def test_eof_checksum_failure_never_publishes_or_marks_review_ready(self):
        row = parse_csv(csv_content(), "UTC")[0].raw
        batch = self.batch(csv_content(row, row | {"Heading": "E"}), prepare=False)
        with batch.document.file.storage.open(
            batch.document.file.name, "wb"
        ) as original:
            original.write(csv_content(row, row | {"Heading": "W"}))
        with patch("geo.vehicle_import_jobs.CHUNK_SIZE", 1):
            self.assertEqual(self.run_job(batch), "failed")
        self.assertEqual(batch.error_code, "source_invalid")
        self.assertEqual(batch.row_count, 0)
        self.assertIsNone(batch.completed_at)
        self.assertFalse(batch.observations.exists())

    def test_job_deadline_groups_and_operator_byte_bounds_fail_recoverably(self):
        deadline = self.batch(prepare=False)
        with patch("geo.vehicle_import_jobs.MAX_JOB_SECONDS", -1):
            self.assertEqual(self.run_job(deadline), "failed")
        self.assertEqual(deadline.error_code, "limit_exceeded")
        row = parse_csv(csv_content(), "UTC")[0].raw
        groups = self.batch(
            csv_content(row, row | {"Asset": "Another asset"}), prepare=False
        )
        with patch("geo.vehicle_import_jobs.MAX_GROUPS", 1):
            self.assertEqual(self.run_job(groups), "failed")
        self.assertEqual(groups.error_code, "limit_exceeded")
        byte_limit = self.batch(prepare=False)
        with self.settings(PANDECT_MAX_FILE_BYTES=100):
            self.assertEqual(self.run_job(byte_limit), "failed")
        self.assertEqual(byte_limit.error_code, "source_invalid")
        self.assertFalse(VehicleLocationEvidence.objects.exists())

    def test_cross_case_source_corruption_records_safe_failure_after_claim(self):
        batch = self.batch(prepare=False)
        DocumentEvidence.objects.filter(pk=batch.document_id).update(
            case=self.other_case
        )
        self.assertEqual(self.run_job(batch), "failed")
        self.assertEqual(batch.error_code, "source_invalid")
        self.assertFalse(batch.rows.exists())
        self.assertFalse(VehicleLocationEvidence.objects.exists())

    def test_access_removed_during_final_chunk_rolls_back_publication(self):
        batch = self.batch()
        self.queue_completion(batch)
        create = VehicleLocationEvidence.objects.bulk_create

        def revoke_after_insert(items, **kwargs):
            result = create(items, **kwargs)
            self.case.memberships.filter(user=self.users["editor"]).update(
                role="viewer"
            )
            return result

        with patch.object(
            VehicleLocationEvidence.objects,
            "bulk_create",
            side_effect=revoke_after_insert,
        ):
            self.assertEqual(self.run_job(batch), "failed")
        self.assertEqual(batch.error_code, "access_removed")
        self.assertIsNone(batch.completed_at)
        self.assertFalse(batch.observations.exists())

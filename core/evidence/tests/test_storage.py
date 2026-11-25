"""Model failures cannot leave new files behind or delete retained originals."""

from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.test import TestCase

from core.analysis.errors import AnalysisError
from core.analysis.sources import Source
from core.analysis.testing import preview_image_bytes
from core.models import PendingFileDeletion
from core.parsers import read_source
from core.storage import _measure_file, staged_file_upload
from core.testing import FILE_MODELS, EvidenceTestMixin, EvidenceViewTestMixin
from document.models import DocumentEvidence
from image.models import ImageEvidence
from phone.models import CallEvidenceAttachment, PhoneCallEvidence


class OriginalSaveCleanupTests(EvidenceTestMixin, TestCase):
    def test_request_upload_is_measured_once_across_validation_and_save(self):
        upload = SimpleUploadedFile("document.txt", b"Exact synthetic statement.")
        item = self.new_evidence(DocumentEvidence, file=upload)
        with (
            patch("core.storage._measure_file", wraps=_measure_file) as measure,
            staged_file_upload(upload, limit=4096),
        ):
            item.full_clean()
            item.save()
        measure.assert_called_once_with(upload)

    def test_committed_storage_failure_remains_retryable_after_row_deletion(self):
        item = self.create_evidence(DocumentEvidence)
        storage, key = item.file.storage, item.file.name
        with (
            patch.object(storage, "delete", side_effect=OSError("Backend error")),
            self.assertLogs("core.storage", level="ERROR"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            item.delete()
            self.assertTrue(
                PendingFileDeletion.objects.filter(storage_key=key).exists()
            )
        self.assertTrue(storage.exists(key))
        call_command("retry_file_cleanup", "--dry-run")
        self.assertTrue(storage.exists(key))
        call_command("retry_file_cleanup")
        self.assertFalse(storage.exists(key))
        self.assertFalse(PendingFileDeletion.objects.filter(storage_key=key).exists())

    def test_failed_cleanup_allows_later_keys_to_be_retried(self):
        pending = PendingFileDeletion.objects
        prefix = f"evidence/cases/{self.case.pk}/documentevidence"
        stuck = pending.create(case_id=self.case.pk, storage_key=f"{prefix}/first")
        healthy = pending.create(case_id=self.case.pk, storage_key=f"{prefix}/later")
        with (
            patch("core.storage.default_storage.delete", side_effect=[OSError, None]),
            self.assertLogs("core.storage", level="ERROR"),
        ):
            call_command("retry_file_cleanup", "--limit", 1)
            call_command("retry_file_cleanup", "--limit", 1)
        stuck.refresh_from_db()
        self.assertIsNotNone(stuck.last_attempted_at)
        self.assertFalse(PendingFileDeletion.objects.filter(pk=healthy.pk).exists())

    def test_cleanup_requires_independent_case_ownership(self):
        retained = self.create_evidence(DocumentEvidence, case=self.other_case)
        for case_id in (None, self.case.pk):
            PendingFileDeletion.objects.create(
                case_id=case_id, storage_key=retained.file.name
            )
        with (
            patch.object(retained.file.storage, "delete") as delete,
            self.assertLogs("core.storage", level="WARNING"),
        ):
            call_command("retry_file_cleanup")
        delete.assert_not_called()
        self.assertEqual(PendingFileDeletion.objects.count(), 2)
        self.assertTrue(retained.file.storage.exists(retained.file.name))

    def test_inconsistent_storage_reference_cannot_schedule_cleanup(self):
        item = self.create_evidence(DocumentEvidence)
        retained = self.create_evidence(DocumentEvidence, case=self.other_case)
        DocumentEvidence.objects.filter(pk=item.pk).update(file=retained.file.name)
        item.refresh_from_db()
        with self.assertRaises(ValidationError), transaction.atomic():
            item.delete()
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertFalse(PendingFileDeletion.objects.exists())
        self.assertTrue(retained.file.storage.exists(retained.file.name))

    def test_failed_file_insert_rolls_back_and_removes_only_its_new_file(self):
        retained = self.create_evidence(DocumentEvidence)
        for model in FILE_MODELS:
            with self.subTest(model=model.__name__):
                item = self.new_evidence(model, title="")
                with self.assertRaises(IntegrityError):
                    item.save()
                self.assertFalse(model.objects.filter(pk=item.pk).exists())
                self.assertFalse(item.file.storage.exists(item.file.name))
                self.assertTrue(retained.file.storage.exists(retained.file.name))

    def test_failed_attachment_insert_preserves_previously_retained_material(self):
        call = self.create_evidence(PhoneCallEvidence)
        submission = uuid4()
        retained = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.editor,
            submission_id=submission,
            transcript="Retained transcript",
        )
        item = CallEvidenceAttachment(
            case=self.case,
            phone_call=call,
            uploaded_by=self.editor,
            submission_id=submission,
            file=SimpleUploadedFile("recording.wav", b"synthetic audio"),
        )
        with self.assertRaises(IntegrityError):
            item.save()
        self.assertFalse(CallEvidenceAttachment.objects.filter(pk=item.pk).exists())
        self.assertFalse(item.file.storage.exists(item.file.name))
        retained.refresh_from_db()
        self.assertEqual(retained.transcript, "Retained transcript")

    def test_signal_failure_rolls_back_record_before_removing_original(self):
        call = self.create_evidence(PhoneCallEvidence)
        items = [
            self.new_evidence(DocumentEvidence),
            CallEvidenceAttachment(
                case=self.case,
                phone_call=call,
                uploaded_by=self.editor,
                file=SimpleUploadedFile("recording.wav", b"synthetic audio"),
            ),
        ]

        def fail_after_insert(**kwargs):
            raise RuntimeError("Synthetic post-save failure")

        for item in items:
            model = type(item)
            with self.subTest(model=model.__name__):
                post_save.connect(fail_after_insert, sender=model)
                try:
                    with self.assertRaisesMessage(RuntimeError, "post-save failure"):
                        item.save()
                finally:
                    post_save.disconnect(fail_after_insert, sender=model)
                self.assertFalse(model.objects.filter(pk=item.pk).exists())
                self.assertFalse(item.file.storage.exists(item.file.name))

    def test_failed_metadata_update_never_removes_existing_original(self):
        item = self.create_evidence(DocumentEvidence)
        item.title = ""
        with self.assertRaises(IntegrityError), transaction.atomic():
            item.save()
        item.refresh_from_db()
        self.assertTrue(item.file.storage.exists(item.file.name))

    def test_cleanup_failure_keeps_primary_error_and_logs_only_storage_key(self):
        item = self.new_evidence(DocumentEvidence, title="")
        with (
            patch.object(
                item.file.storage,
                "delete",
                side_effect=OSError("Private backend detail"),
            ),
            self.assertLogs("core.storage", level="ERROR") as logs,
            self.assertRaises(IntegrityError),
        ):
            item.save()
        self.assertFalse(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertTrue(item.file.storage.exists(item.file.name))
        self.assertIn(item.file.name, logs.output[0])
        self.assertNotIn("original.bin", logs.output[0])
        self.assertNotIn("Private backend detail", logs.output[0])


class OriginalReadIsolationTests(EvidenceViewTestMixin, TestCase):
    def test_download_preview_and_parsers_require_case_owned_storage(self):
        item, other = (
            ImageEvidence.objects.create(
                case=case,
                uploaded_by=case.owner,
                title="Retained image",
                file=SimpleUploadedFile("capture.png", preview_image_bytes()),
            )
            for case in (self.case, self.other_case)
        )
        self.assertEqual(item.sha256, other.sha256)
        ImageEvidence.objects.filter(pk=item.pk).update(file=other.file.name)
        item.refresh_from_db()
        self.login("owner")
        with patch.object(item.file.storage, "open") as storage_open:
            self.assertEqual(
                self.client.get(self.url("download", item, kind="image")).status_code,
                503,
            )
            self.assertEqual(
                self.client.get(self.url("preview", item, kind="image")).status_code,
                422,
            )
            with self.assertRaises(AnalysisError) as error:
                read_source(Source(str(item.pk), "image", item))
        self.assertEqual(error.exception.code, "source_unavailable")
        storage_open.assert_not_called()

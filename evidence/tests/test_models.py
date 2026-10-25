import hashlib
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from cases.models import Case, CaseMembership
from evidence.models import (
    AudioEvidence,
    DocumentEvidence,
    ImageEvidence,
    TextEvidence,
)

FILE_MODELS = (ImageEvidence, AudioEvidence, DocumentEvidence)
ALL_MODELS = (*FILE_MODELS, TextEvidence)
ORIGINAL = b"Synthetic original material\x00\r\n"


class EvidenceTestMixin:
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(username="owner")
        cls.editor = get_user_model().objects.create_user(username="editor")
        cls.viewer = get_user_model().objects.create_user(username="viewer")
        cls.outsider = get_user_model().objects.create_user(username="outsider")
        cls.case = Case.objects.create(owner=cls.owner)
        cls.other_case = Case.objects.create(owner=cls.outsider)
        CaseMembership.objects.create(
            case=cls.case, user=cls.editor, role=CaseMembership.Role.EDITOR
        )
        CaseMembership.objects.create(case=cls.case, user=cls.viewer)

    def setUp(self):
        directory = TemporaryDirectory()
        self.media_root = Path(directory.name)
        self.addCleanup(directory.cleanup)
        settings = self.settings(MEDIA_ROOT=self.media_root)
        settings.enable()
        self.addCleanup(settings.disable)

    def new_evidence(self, model=TextEvidence, **overrides):
        values = {
            "case": self.case,
            "uploaded_by": self.editor,
            "title": "Synthetic material",
        }
        if model is TextEvidence:
            values["body"] = "Original café\r\nincluding spacing.  "
        else:
            values["file"] = SimpleUploadedFile(
                "original.bin", ORIGINAL, content_type="application/octet-stream"
            )
        return model(**(values | overrides))

    def create_evidence(self, model=TextEvidence, **overrides):
        evidence = self.new_evidence(model, **overrides)
        evidence.save()
        return evidence


class EvidenceModelTests(EvidenceTestMixin, TestCase):
    def test_all_material_types_belong_to_one_case_and_preserve_uploader(self):
        for model in ALL_MODELS:
            with self.subTest(model=model.__name__):
                evidence = self.new_evidence(model)
                evidence.full_clean()
                evidence.save()
                evidence.refresh_from_db()
                self.assertEqual(evidence.case, self.case)
                self.assertEqual(evidence.uploaded_by, self.editor)
                self.assertEqual(str(evidence), evidence.title)
                self.assertEqual(model.objects.filter(case=self.case).get(), evidence)
                self.assertFalse(model.objects.filter(case=self.other_case).exists())
                reverse_name = model._meta.get_field("case").remote_field.related_name
                self.assertEqual(getattr(self.case, reverse_name).get(), evidence)
                self.assertFalse(admin.site.is_registered(model))

    def test_case_and_uploader_are_required_by_validation_and_database(self):
        for model in ALL_MODELS:
            evidence = self.create_evidence(model)
            for field in ("case", "uploaded_by"):
                with self.subTest(model=model.__name__, field=field):
                    invalid = self.new_evidence(model, **{field: None})
                    with self.assertRaises(ValidationError):
                        invalid.full_clean()
                    with self.assertRaises(ValidationError):
                        invalid.save()
                    with transaction.atomic(), self.assertRaises(IntegrityError):
                        model.objects.filter(pk=evidence.pk).update(**{field: None})

    def test_original_file_bytes_and_generated_metadata_are_preserved(self):
        for model in FILE_MODELS:
            with self.subTest(model=model.__name__):
                evidence = self.create_evidence(
                    model, sha256="f" * 64, byte_size=1, original_filename="forged"
                )
                evidence.refresh_from_db()
                self.assertEqual(evidence.sha256, hashlib.sha256(ORIGINAL).hexdigest())
                self.assertEqual(evidence.byte_size, len(ORIGINAL))
                self.assertEqual(evidence.original_filename, "original.bin")
                self.assertEqual(
                    evidence.declared_content_type, "application/octet-stream"
                )
                with evidence.file.open("rb") as original:
                    self.assertEqual(original.read(), ORIGINAL)

    def test_text_checksum_uses_exact_utf8_without_normalizing(self):
        evidence = self.create_evidence(sha256="f" * 64)
        evidence.refresh_from_db()
        self.assertEqual(evidence.body, "Original café\r\nincluding spacing.  ")
        self.assertEqual(
            evidence.sha256, hashlib.sha256(evidence.body.encode("utf-8")).hexdigest()
        )

    def test_content_type_can_be_unknown_or_supplied_without_an_uploaded_type(self):
        for declared in ("", "text/plain"):
            with self.subTest(declared=declared):
                evidence = self.create_evidence(
                    DocumentEvidence,
                    file=SimpleUploadedFile(
                        "original.bin", ORIGINAL, content_type=None
                    ),
                    declared_content_type=declared,
                )
                evidence.refresh_from_db()
                self.assertEqual(evidence.declared_content_type, declared)

    def test_hashing_is_chunked_and_preserves_input_position(self):
        content = ContentFile(b"x" * (150 * 1024), name="large.bin")
        content.seek(7)
        evidence = self.new_evidence(DocumentEvidence, file=content)
        with patch.object(content.file, "read", wraps=content.file.read) as read:
            evidence.full_clean()
        self.assertEqual(content.tell(), 7)
        self.assertTrue(read.call_args_list)
        self.assertTrue(all(call.args == (64 * 1024,) for call in read.call_args_list))
        evidence.save()
        with evidence.file.open("rb") as original:
            self.assertEqual(
                hashlib.sha256(original.read()).hexdigest(), evidence.sha256
            )

    def test_metadata_edits_update_timestamp_without_reading_original(self):
        for model in ALL_MODELS:
            with self.subTest(model=model.__name__):
                evidence = self.create_evidence(model)
                created_at, edited_at = evidence.created_at, evidence.edited_at
                checksum = evidence.sha256
                evidence.description = "Analyst description"
                later = timezone.now() + timedelta(days=1)
                with patch("django.utils.timezone.now", return_value=later):
                    if model is TextEvidence:
                        evidence.save(update_fields=["description"])
                    else:
                        with patch.object(evidence.file.storage, "open") as opened:
                            evidence.save(update_fields=["description"])
                            opened.assert_not_called()
                evidence.refresh_from_db()
                self.assertEqual(evidence.description, "Analyst description")
                self.assertEqual(evidence.created_at, created_at)
                self.assertEqual(evidence.edited_at, later)
                self.assertGreater(evidence.edited_at, edited_at)
                self.assertEqual(evidence.sha256, checksum)

    def test_original_identity_and_integrity_changes_are_rejected(self):
        for model in ALL_MODELS:
            evidence = self.create_evidence(model)
            changes = {
                "case": self.other_case,
                "uploaded_by": self.owner,
                "created_at": evidence.created_at - timedelta(days=1),
                "sha256": "0" * 64,
            }
            if model is TextEvidence:
                changes["body"] = "Replacement text"
            else:
                changes |= {
                    "file": "evidence/another-case/original",
                    "original_filename": "replacement.bin",
                    "byte_size": 999,
                    "declared_content_type": "text/plain",
                }
            for field, value in changes.items():
                with self.subTest(model=model.__name__, field=field):
                    updated = model.objects.get(pk=evidence.pk)
                    setattr(updated, field, value)
                    with self.assertRaises(ValidationError):
                        updated.full_clean()
                    with self.assertRaises(ValidationError):
                        updated.save(update_fields=["description"])
            evidence.refresh_from_db()
            self.assertEqual(evidence.case, self.case)
            self.assertEqual(evidence.uploaded_by, self.editor)

    def test_deferred_metadata_save_updates_edit_timestamp(self):
        evidence = self.create_evidence()
        deferred = TextEvidence.objects.only("title").get(pk=evidence.pk)
        deferred.title = "Revised description title"
        later = timezone.now() + timedelta(days=1)
        with patch("django.utils.timezone.now", return_value=later):
            deferred.save()
        evidence.refresh_from_db()
        self.assertEqual(evidence.title, deferred.title)
        self.assertEqual(evidence.edited_at, later)
        self.assertEqual(evidence.body, deferred.body)

    def test_same_name_replacement_is_rejected_before_storage_write(self):
        for model in FILE_MODELS:
            with self.subTest(model=model.__name__):
                evidence = self.create_evidence(model)
                key = evidence.file.name
                evidence.file = ContentFile(b"Replacement", name=key)
                with self.assertRaises(ValidationError):
                    evidence.save()
                evidence.refresh_from_db()
                self.assertEqual(evidence.file.name, key)
                with evidence.file.open("rb") as original:
                    self.assertEqual(original.read(), ORIGINAL)
        self.assertEqual(sum(path.is_file() for path in self.media_root.rglob("*")), 3)

    def test_stored_file_paths_cannot_be_reused_for_new_evidence(self):
        original = self.create_evidence(DocumentEvidence)
        for value in (original.file.name, original.file):
            with self.subTest(value=type(value).__name__):
                evidence = self.new_evidence(
                    DocumentEvidence, case=self.other_case, file=value
                )
                with self.assertRaises(ValidationError):
                    evidence.save()

    def test_storage_paths_are_unique_case_scoped_and_ignore_original_paths(self):
        keys = set()
        for case in (self.case, self.other_case):
            for model in FILE_MODELS:
                for _ in range(2):
                    evidence = self.create_evidence(
                        model,
                        case=case,
                        file=ContentFile(ORIGINAL, name="../../private/original.bin"),
                    )
                    self.assertTrue(
                        evidence.file.name.startswith(
                            f"evidence/cases/{case.pk}/{model._meta.model_name}/"
                        )
                    )
                    self.assertNotIn("original.bin", evidence.file.name)
                    self.assertNotIn("..", evidence.file.name)
                    self.assertEqual(evidence.original_filename, "original.bin")
                    keys.add(evidence.file.name)
        self.assertEqual(len(keys), 12)

    def test_missing_or_empty_originals_are_rejected(self):
        for model in FILE_MODELS:
            for file in (None, ContentFile(b"", name="empty.bin")):
                with self.subTest(model=model.__name__, file=file):
                    evidence = self.new_evidence(model, file=file)
                    with self.assertRaises(ValidationError):
                        evidence.full_clean()
                    with self.assertRaises(ValidationError):
                        evidence.save()
        with self.assertRaises(ValidationError):
            self.new_evidence(body="").save()
        self.assertFalse(any(self.media_root.iterdir()))

    def test_database_rejects_missing_content_title_and_invalid_checksum(self):
        for model in ALL_MODELS:
            evidence = self.create_evidence(model)
            fields = {"title": "", "sha256": "not-a-checksum"}
            if model is TextEvidence:
                fields["body"] = ""
            else:
                fields |= {"file": "", "original_filename": "", "byte_size": 0}
            for field, value in fields.items():
                with self.subTest(model=model.__name__, field=field):
                    with transaction.atomic(), self.assertRaises(IntegrityError):
                        model.objects.filter(pk=evidence.pk).update(**{field: value})

    def test_optional_type_metadata_validates_and_has_database_bounds(self):
        for model, fields in (
            (ImageEvidence, {"width": 1, "height": 1}),
            (
                AudioEvidence,
                {"duration": timedelta(), "sample_rate": 48000, "channel_count": 2},
            ),
            (DocumentEvidence, {"page_count": 1}),
        ):
            evidence = self.create_evidence(model)
            evidence.full_clean()
            for field in fields:
                self.assertIsNone(getattr(evidence, field))
            for field, valid in fields.items():
                with self.subTest(model=model.__name__, field=field):
                    setattr(evidence, field, valid)
                    evidence.full_clean()
                    evidence.save()
                    invalid = timedelta(microseconds=-1) if field == "duration" else 0
                    setattr(evidence, field, invalid)
                    with self.assertRaises(ValidationError):
                        evidence.full_clean()
                    with transaction.atomic(), self.assertRaises(IntegrityError):
                        model.objects.filter(pk=evidence.pk).update(**{field: invalid})
                    setattr(evidence, field, valid)

    def test_uploader_is_protected_after_access_is_revoked(self):
        records = [self.create_evidence(model) for model in ALL_MODELS]
        self.case.memberships.filter(user=self.editor).delete()
        for evidence in records:
            evidence.full_clean()
            evidence.description = "Retained historical attribution"
            evidence.save()
            self.assertEqual(evidence.uploaded_by, self.editor)
        for delete in (
            self.editor.delete,
            get_user_model().objects.filter(pk=self.editor.pk).delete,
        ):
            with self.assertRaises(ProtectedError):
                delete()

    def test_deleted_evidence_cannot_be_resurrected_by_stale_save(self):
        evidence = self.create_evidence()
        TextEvidence.objects.filter(pk=evidence.pk).delete()
        with self.assertRaises(ValidationError):
            evidence.save()
        self.assertFalse(TextEvidence.objects.exists())

    def test_explicit_ids_cannot_replace_existing_records(self):
        evidence = self.create_evidence()
        replacement = self.new_evidence(pk=evidence.pk, body="Replacement text")
        with self.assertRaises(ValidationError):
            replacement.save()
        evidence.refresh_from_db()
        self.assertNotEqual(evidence.body, replacement.body)

    def test_newest_records_are_listed_first(self):
        first = self.create_evidence()
        second = self.create_evidence()
        self.assertEqual(list(TextEvidence.objects.all()), [second, first])


class EvidenceDeletionTests(EvidenceTestMixin, TestCase):
    def test_individual_and_queryset_deletion_wait_for_commit(self):
        for model in FILE_MODELS:
            for queryset in (False, True):
                with self.subTest(model=model.__name__, queryset=queryset):
                    evidence = self.create_evidence(model)
                    storage, name = evidence.file.storage, evidence.file.name
                    with self.captureOnCommitCallbacks(execute=True) as callbacks:
                        if queryset:
                            model.objects.filter(pk=evidence.pk).delete()
                        else:
                            evidence.delete()
                        self.assertTrue(storage.exists(name))
                    self.assertEqual(len(callbacks), 1)
                    self.assertFalse(storage.exists(name))

    def test_case_deletion_removes_all_its_files_and_preserves_other_cases(self):
        files = [self.create_evidence(model).file for model in FILE_MODELS]
        text = self.create_evidence()
        retained = self.create_evidence(DocumentEvidence, case=self.other_case)
        with self.captureOnCommitCallbacks(execute=True):
            self.case.delete()
            self.assertTrue(all(file.storage.exists(file.name) for file in files))
        self.assertTrue(all(not file.storage.exists(file.name) for file in files))
        self.assertFalse(TextEvidence.objects.filter(pk=text.pk).exists())
        self.assertTrue(retained.file.storage.exists(retained.file.name))
        retained.refresh_from_db()
        self.assertTrue(get_user_model().objects.filter(pk=self.editor.pk).exists())

    def test_rolled_back_deletion_preserves_record_and_file(self):
        evidence = self.create_evidence(DocumentEvidence)
        storage, name, pk = evidence.file.storage, evidence.file.name, evidence.pk
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with (
                self.assertRaisesMessage(RuntimeError, "rollback"),
                transaction.atomic(),
            ):
                DocumentEvidence.objects.filter(pk=pk).delete()
                raise RuntimeError("rollback")
        self.assertEqual(callbacks, [])
        self.assertTrue(storage.exists(name))
        self.assertTrue(DocumentEvidence.objects.filter(pk=pk).exists())

    def test_storage_failures_are_logged_without_blocking_other_cleanup(self):
        failed = self.create_evidence(DocumentEvidence)
        successful = self.create_evidence(ImageEvidence)
        storage = failed.file.storage
        real_delete = storage.delete
        failed_key, successful_key = failed.file.name, successful.file.name

        def delete(name):
            if name == failed_key:
                raise OSError("Sensitive backend exception")
            real_delete(name)

        with (
            patch.object(storage, "delete", side_effect=delete),
            self.assertLogs("evidence.signals", level="ERROR") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.case.delete()
        self.assertTrue(storage.exists(failed_key))
        self.assertFalse(storage.exists(successful_key))
        self.assertFalse(DocumentEvidence.objects.filter(pk=failed.pk).exists())
        self.assertIn(failed_key, logs.output[0])
        self.assertNotIn("original.bin", logs.output[0])
        self.assertNotIn("Sensitive backend exception", logs.output[0])

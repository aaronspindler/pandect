"""Confirmed superuser deletion removes originals and dependent analysis files."""

import json
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from cases.permissions import get_case
from core.analysis.operations import get_operation
from core.analysis.worker import process_run
from core.evidence import deletion
from core.evidence.types import KINDS
from core.models import (
    AnalysisArtifact,
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
    ExtractedPassage,
    GatewayRequest,
)
from core.testing import ALL_MODELS, EvidenceTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationImport
from phone.models import CallEvidenceAttachment


class SuperuserEvidenceDeletionTests(EvidenceTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.operator = get_user_model().objects.create_superuser(
            username="operator", email="operator@example.com", password=None
        )
        self.membership = CaseMembership.objects.create(
            case=self.case, user=self.operator, role=CaseMembership.Role.VIEWER
        )
        self.client.force_login(self.operator)

    @staticmethod
    def url(item, action="delete"):
        return reverse(
            f"admin:{item._meta.app_label}_{item._meta.model_name}_{action}",
            args=[item.pk],
        )

    def confirmation(self, item):
        response = self.client.get(self.url(item))
        revision = (
            response.context.get("deletion_revision", "")
            if response.status_code == 200
            else ""
        )
        return {"post": "yes", "revision": revision}

    def analysis(self, item, *, inputs=None):
        kind = next(k for k, model in KINDS.items() if isinstance(item, model))
        run = AnalysisRun.objects.create(
            case=item.case,
            requested_by=self.operator,
            operation="native_text",
            method_version="test.v1",
            inputs=inputs
            or [{"id": str(uuid4()), "kind": kind, "evidence_id": str(item.pk)}],
            status="complete",
        )
        result = AnalysisResult.objects.create(case=item.case, run=run)
        artifact = AnalysisArtifact.objects.create(
            case=item.case,
            result=result,
            source_id=run.inputs[0]["id"],
            file=SimpleUploadedFile("preview.jpg", b"Synthetic preview"),
            sha256="a" * 64,
            width=1,
            height=1,
        )
        ExtractedPassage.objects.create(
            case=item.case,
            result=result,
            kind=kind,
            evidence_id=item.pk,
            evidence_sha256=item.sha256,
            text="Synthetic extracted content",
        )
        return run, result, artifact.file

    def test_superuser_confirms_deletion_of_every_type_including_archived(self):
        for model in ALL_MODELS:
            with self.subTest(model=model.__name__):
                item = self.create_evidence(
                    model,
                    is_archived=True,
                    archive_changed_at=timezone.now(),
                    archive_changed_by=self.operator,
                )
                retained = self.create_evidence(model)
                files = [item.file] if getattr(item, "file", None) else []
                if model in (KINDS["phone-call"], KINDS["facetime"]):
                    attachment = CallEvidenceAttachment.objects.create(
                        case=self.case,
                        uploaded_by=self.editor,
                        **{
                            "phone_call"
                            if model is KINDS["phone-call"]
                            else "facetime": item
                        },
                        file=SimpleUploadedFile("call.wav", b"Synthetic recording"),
                        transcript="Supplied transcript",
                    )
                    files.append(attachment.file)
                    _, _, artifact = self.analysis(
                        item,
                        inputs=[
                            {
                                "id": str(uuid4()),
                                "kind": "phone-call"
                                if model is KINDS["phone-call"]
                                else "facetime",
                                "evidence_id": str(item.pk),
                                "attachment_id": str(attachment.pk),
                            }
                        ],
                    )
                    files.append(artifact)
                url = self.url(item)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "This permanently deletes")
                self.assertContains(response, item.title)
                self.assertTrue(model.objects.filter(pk=item.pk).exists())
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(url, self.confirmation(item))
                    for original in files:
                        self.assertTrue(original.storage.exists(original.name))
                self.assertEqual(response.status_code, 302)
                self.assertFalse(model.objects.filter(pk=item.pk).exists())
                self.assertTrue(model.objects.filter(pk=retained.pk).exists())
                for original in files:
                    self.assertFalse(original.storage.exists(original.name))
                self.assertTrue(
                    LogEntry.objects.filter(
                        user=self.operator, object_id=str(item.pk), action_flag=DELETION
                    ).exists()
                )

    def test_removes_whole_multisource_runs_and_downstream_analysis_only(self):
        item = self.create_evidence(DocumentEvidence)
        retained = self.create_evidence(DocumentEvidence)
        hidden = self.create_evidence(DocumentEvidence, case=self.other_case)
        first, result, first_file = self.analysis(
            item,
            inputs=[
                {
                    "id": str(uuid4()),
                    "kind": "document",
                    "evidence_id": str(source.pk),
                    "sha256": source.sha256,
                }
                for source in (item, retained)
            ],
        )
        review = AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.operator,
            decision=AnalysisReview.Decision.UNRESOLVED,
        )
        reference = AnalysisArtifact.objects.create(
            case=self.case,
            result=result,
            source_id=first.inputs[0]["id"],
            kind=AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE,
            file=SimpleUploadedFile("reference.json", b"{}"),
            sha256="b" * 64,
        ).file
        downstream, _, second_file = self.analysis(
            retained,
            inputs=[
                {
                    "id": str(uuid4()),
                    "kind": "document",
                    "evidence_id": str(retained.pk),
                    "result_id": str(result.pk),
                }
            ],
        )
        unrelated, _, unrelated_file = self.analysis(retained)
        other_case, _, hidden_file = self.analysis(hidden, inputs=first.inputs)
        queued = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.operator,
            operation="native_text",
            method_version="test.v1",
            inputs=first.inputs,
        )
        response = self.client.get(self.url(item))
        self.assertIn(("Analysis run", 3), response.context["deletion_record_counts"])
        self.assertNotContains(response, str(first.pk))
        manifest = self.client.get(
            self.url(item) + response.context["deletion_download_url"]
        )
        inventory = json.loads(b"".join(manifest.streaming_content))
        ids = {entry["id"] for entry in inventory["entries"]}
        self.assertTrue({str(first.pk), str(downstream.pk), str(queued.pk)} <= ids)
        self.assertNotIn(str(unrelated.pk), ids)
        self.assertNotIn(str(other_case.pk), ids)
        self.assertNotContains(response, str(unrelated.pk))
        self.assertNotContains(response, str(other_case.pk))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url(item), self.confirmation(item))
        self.assertFalse(
            AnalysisRun.objects.filter(
                pk__in=[first.pk, downstream.pk, queued.pk]
            ).exists()
        )
        self.assertEqual(ExtractedPassage.objects.filter(case=self.case).count(), 1)
        self.assertFalse(AnalysisReview.objects.filter(pk=review.pk).exists())
        for original in (item.file, first_file, second_file, reference):
            self.assertFalse(original.storage.exists(original.name))
        for original in (retained.file, unrelated_file, hidden_file):
            self.assertTrue(original.storage.exists(original.name))
        self.assertEqual(process_run(queued.pk), "unavailable")

    def test_running_analysis_cannot_publish_after_original_is_deleted(self):
        self.membership.role = CaseMembership.Role.EDITOR
        self.membership.save()
        EmailAddress.objects.create(
            user=self.operator, email=self.operator.email, verified=True
        )
        item = self.create_evidence(DocumentEvidence)
        operation = get_operation("native_text")
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.operator,
            operation="native_text",
            method_version=operation.version,
            inputs=[
                {
                    "id": str(uuid4()),
                    "kind": "document",
                    "evidence_id": str(item.pk),
                    "sha256": item.sha256,
                }
            ],
        )

        def finish_after_delete(sources, parameters):
            response = self.client.post(self.url(item), self.confirmation(item))
            self.assertEqual(response.status_code, 302)
            return {"summary": "Derived content", "items": []}

        with patch(
            "core.analysis.worker.get_operation",
            return_value=replace(operation, function=finish_after_delete),
        ):
            process_run(run.pk)
        self.assertFalse(AnalysisRun.objects.filter(pk=run.pk).exists())
        self.assertFalse(AnalysisResult.objects.filter(run_id=run.pk).exists())
        self.assertFalse(DocumentEvidence.objects.filter(pk=item.pk).exists())

    def test_current_case_access_is_required_on_confirmation(self):
        item = self.create_evidence(DocumentEvidence)
        self.assertEqual(self.client.get(self.url(item)).status_code, 200)
        self.membership.delete()
        self.assertEqual(
            self.client.post(self.url(item), self.confirmation(item)).status_code, 302
        )
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertTrue(item.file.storage.exists(item.file.name))

    def test_staff_model_permissions_never_grant_individual_delete(self):
        item = self.create_evidence()
        for user in (self.owner, self.editor, self.viewer, self.outsider):
            with self.subTest(user=user):
                user.is_staff = True
                user.save(update_fields=["is_staff"])
                user.user_permissions.set(
                    Permission.objects.filter(
                        content_type__app_label__in=(
                            "text",
                            "image",
                            "audio",
                            "document",
                            "geo",
                            "phone",
                        )
                    )
                )
                self.client.force_login(user)
                self.assertEqual(
                    self.client.post(
                        self.url(item), self.confirmation(item)
                    ).status_code,
                    403,
                )
        self.assertTrue(type(item).objects.filter(pk=item.pk).exists())

    def test_superuser_cannot_delete_cross_case_objects_or_use_bulk_delete(self):
        item = self.create_evidence(DocumentEvidence, case=self.other_case)
        self.assertEqual(self.client.get(self.url(item)).status_code, 302)
        self.client.post(self.url(item), self.confirmation(item))
        own = self.create_evidence(DocumentEvidence)
        response = self.client.post(
            reverse("admin:document_documentevidence_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": own.pk,
                "post": "yes",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(DocumentEvidence.objects.filter(pk=own.pk).exists())
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())

    def test_csrf_is_required(self):
        item = self.create_evidence(DocumentEvidence)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        self.assertEqual(client.post(self.url(item), {"post": "yes"}).status_code, 403)
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())

    def test_import_source_deletion_includes_its_import_and_analysis(self):
        item = self.create_evidence(DocumentEvidence)
        VehicleLocationImport.objects.create(
            case=self.case, uploaded_by=item.uploaded_by, document=item, timezone="UTC"
        )
        run, _, artifact = self.analysis(item)
        response = self.client.post(self.url(item), self.confirmation(item))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertFalse(AnalysisRun.objects.filter(pk=run.pk).exists())

    def test_foreign_case_call_attachment_is_never_disclosed_or_deleted(self):
        item = self.create_evidence(KINDS["phone-call"])
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=item,
            uploaded_by=self.editor,
            file=SimpleUploadedFile("hidden-case-recording.wav", b"Synthetic audio"),
        )
        # Simulate an inconsistent trusted maintenance write, which normal
        # inline and download queries already hide.
        CallEvidenceAttachment.objects.filter(pk=attachment.pk).update(
            case=self.other_case
        )
        for method in (self.client.get, self.client.post):
            response = method(self.url(item), {"post": "yes"})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "inconsistent case links")
            self.assertNotContains(response, attachment.original_filename)
        self.assertTrue(type(item).objects.filter(pk=item.pk).exists())
        self.assertTrue(
            CallEvidenceAttachment.objects.filter(pk=attachment.pk).exists()
        )
        self.assertTrue(attachment.file.storage.exists(attachment.file.name))

    def test_foreign_case_analysis_descendants_block_deletion(self):
        item = self.create_evidence(DocumentEvidence)
        run, result, original = self.analysis(item)
        review = AnalysisReview.objects.create(
            case=self.case,
            result=result,
            reviewed_by=self.operator,
            decision=AnalysisReview.Decision.UNRESOLVED,
        )
        usage = GatewayRequest.objects.create(
            case=self.case,
            run=run,
            model="synthetic",
            request_bytes=1,
        )
        children = [
            result,
            result.artifacts.get(),
            result.passages.get(),
            review,
            usage,
        ]
        for child in children:
            with self.subTest(model=type(child).__name__):
                type(child).objects.filter(pk=child.pk).update(case=self.other_case)
                with self.captureOnCommitCallbacks(execute=True) as callbacks:
                    response = self.client.post(self.url(item), self.confirmation(item))
                self.assertContains(response, "inconsistent case links")
                self.assertEqual(callbacks, [])
                self.assertTrue(type(child).objects.filter(pk=child.pk).exists())
                self.assertTrue(AnalysisRun.objects.filter(pk=run.pk).exists())
                self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
                self.assertTrue(original.storage.exists(original.name))
                type(child).objects.filter(pk=child.pk).update(case=self.case)

    def test_case_consistency_is_rechecked_under_the_case_lock(self):
        item = self.create_evidence(DocumentEvidence)
        run, result, original = self.analysis(item)

        def corrupt_after_confirmation(*args, **kwargs):
            self.assertTrue(kwargs["lock"])
            case = get_case(*args, **kwargs)
            AnalysisResult.objects.filter(pk=result.pk).update(case=self.other_case)
            return case

        confirmation = self.confirmation(item)
        with patch(
            "core.evidence.deletion.get_case", side_effect=corrupt_after_confirmation
        ):
            response = self.client.post(self.url(item), confirmation)
        self.assertContains(response, "inconsistent case links")
        self.assertNotContains(response, str(result.pk))
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertTrue(AnalysisRun.objects.filter(pk=run.pk).exists())
        self.assertTrue(original.storage.exists(original.name))

    def test_rollback_preserves_originals_and_derived_files(self):
        item = self.create_evidence(DocumentEvidence)
        run, _, artifact = self.analysis(item)
        confirmation = self.confirmation(item)
        with (
            self.captureOnCommitCallbacks(execute=True) as callbacks,
            patch.object(
                deletion, "_analysis_runs", wraps=deletion._analysis_runs
            ) as scan,
        ):
            with transaction.atomic():
                response = self.client.post(self.url(item), confirmation)
                self.assertEqual(response.status_code, 302)
                transaction.set_rollback(True)
        scan.assert_called_once()
        self.assertEqual(callbacks, [])
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertTrue(AnalysisRun.objects.filter(pk=run.pk).exists())
        for original in (item.file, artifact):
            self.assertTrue(original.storage.exists(original.name))

    def test_storage_failure_does_not_skip_other_files(self):
        item = self.create_evidence(DocumentEvidence)
        _, _, artifact = self.analysis(item)
        storage = item.file.storage
        delete = storage.delete

        def fail_original(name):
            if name == item.file.name:
                raise OSError("Private storage detail")
            delete(name)

        with (
            patch.object(storage, "delete", side_effect=fail_original),
            self.assertLogs("core.storage", level="ERROR") as logs,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(self.url(item), self.confirmation(item))
        self.assertFalse(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertFalse(artifact.storage.exists(artifact.name))
        self.assertIn(item.file.name, logs.output[0])
        self.assertNotIn("Private storage detail", logs.output[0])

"""In-app superuser deletion retains the existing case and file boundaries."""

from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from analysis.models import AnalysisArtifact, AnalysisResult, AnalysisRun
from cases.models import CaseMembership
from evidence.models import (
    CallEvidenceAttachment,
    DocumentEvidence,
    VehicleLocationImport,
)
from evidence.tests.test_models import ALL_MODELS, EvidenceTestMixin
from evidence.types import CALL_KINDS, KINDS


class EvidenceDeletionViewTests(EvidenceTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        # Product superusers need no staff/admin access.
        self.operator = get_user_model().objects.create_user(
            username="operator", email="operator@example.test", is_superuser=True
        )
        for user in (
            self.operator,
            self.owner,
            self.editor,
            self.viewer,
            self.outsider,
        ):
            EmailAddress.objects.create(
                user=user, email=f"{user.username}@example.test", verified=True
            )
        self.membership = CaseMembership.objects.create(
            case=self.case, user=self.operator
        )
        self.client.force_login(self.operator)

    def url(self, item, name="delete", case=None):
        kind = next(k for k, model in KINDS.items() if isinstance(item, model))
        return reverse(
            f"evidence:{name}",
            kwargs={
                "case_id": (case or item.case).pk,
                "kind": kind,
                "evidence_id": item.pk,
            },
        )

    def test_superuser_can_delete_every_type_and_retained_call_files(self):
        for model in ALL_MODELS:
            with self.subTest(model=model.__name__):
                item = self.create_evidence(model)
                files = [item.file] if getattr(item, "file", None) else []
                kind = next(k for k, candidate in KINDS.items() if candidate is model)
                if kind in CALL_KINDS:
                    attachment = CallEvidenceAttachment.objects.create(
                        case=self.case,
                        uploaded_by=self.editor,
                        **{"phone_call" if kind == "phone-call" else "facetime": item},
                        transcript="Retained transcript",
                        file=SimpleUploadedFile("call.wav", b"Synthetic audio"),
                    )
                    files.append(attachment.file)
                detail = self.client.get(
                    self.url(item, "detail"), {"section": "details"}
                )
                self.assertContains(detail, self.url(item))
                response = self.client.get(self.url(item))
                self.assertContains(response, item.title)
                self.assertIn("private", response["Cache-Control"])
                self.assertIn("no-store", response["Cache-Control"])
                self.assertTrue(model.objects.filter(pk=item.pk).exists())
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(self.url(item), {"confirm": "on"})
                    for original in files:
                        self.assertTrue(original.storage.exists(original.name))
                self.assertRedirects(
                    response, reverse("evidence:list", kwargs={"case_id": self.case.pk})
                )
                self.assertFalse(model.objects.filter(pk=item.pk).exists())
                for original in files:
                    self.assertFalse(original.storage.exists(original.name))
                self.assertTrue(
                    LogEntry.objects.filter(
                        user=self.operator,
                        object_id=str(item.pk),
                        action_flag=DELETION,
                    ).exists()
                )

    def test_confirmation_is_required_and_cancel_preserves_filters(self):
        item = self.create_evidence(DocumentEvidence)
        suffix = "?q=harbour&type=document&state=all&page=2"
        url = self.url(item) + suffix
        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm", response.context["form"].errors)
        self.assertEqual(
            response.context["cancel_url"],
            self.url(item, "detail") + suffix + "&section=details",
        )
        self.assertTrue(item.file.storage.exists(item.file.name))
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"confirm": "on"})
        self.assertRedirects(
            response,
            reverse("evidence:list", kwargs={"case_id": self.case.pk}) + suffix,
        )

    def test_archived_original_and_generated_files_are_deleted(self):
        item = self.create_evidence(
            DocumentEvidence,
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.editor,
        )
        source_id = uuid4()
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.operator,
            operation="native_text",
            method_version="1",
            status="complete",
            inputs=[
                {"id": str(source_id), "kind": "document", "evidence_id": str(item.pk)}
            ],
        )
        result = AnalysisResult.objects.create(case=self.case, run=run)
        artifact = AnalysisArtifact.objects.create(
            case=self.case,
            result=result,
            source_id=source_id,
            file=SimpleUploadedFile("preview.jpg", b"Synthetic preview"),
            sha256="a" * 64,
            width=1,
            height=1,
        )
        self.assertContains(self.client.get(self.url(item)), "1 analysis run")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url(item), {"confirm": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AnalysisRun.objects.filter(pk=run.pk).exists())
        for original in (item.file, artifact.file):
            self.assertFalse(original.storage.exists(original.name))

    def test_ordinary_case_roles_and_staff_permissions_do_not_grant_delete(self):
        item = self.create_evidence()
        for user in (self.owner, self.editor, self.viewer):
            user.is_staff = True
            user.save(update_fields=["is_staff"])
            user.user_permissions.set(
                Permission.objects.filter(content_type__app_label="evidence")
            )
            self.client.force_login(user)
            self.assertNotContains(
                self.client.get(self.url(item, "detail"), {"section": "details"}),
                "Delete evidence",
            )
            for method in (self.client.get, self.client.post):
                self.assertEqual(
                    method(self.url(item), {"confirm": "on"}).status_code, 403
                )
        self.assertTrue(type(item).objects.filter(pk=item.pk).exists())

    def test_current_case_access_and_parent_case_are_required(self):
        item = self.create_evidence(DocumentEvidence)
        self.assertEqual(self.client.get(self.url(item)).status_code, 200)
        self.assertEqual(
            self.client.post(
                self.url(item, case=self.other_case), {"confirm": "on"}
            ).status_code,
            404,
        )
        other = self.create_evidence(DocumentEvidence, case=self.other_case)
        self.assertEqual(
            self.client.get(self.url(other, case=self.case)).status_code, 404
        )
        self.membership.delete()
        self.assertEqual(
            self.client.post(self.url(item), {"confirm": "on"}).status_code, 404
        )
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertTrue(item.file.storage.exists(item.file.name))

    def test_authentication_verification_and_superuser_status_are_current(self):
        item = self.create_evidence()
        self.client.logout()
        self.assertEqual(
            self.client.post(self.url(item), {"confirm": "on"}).status_code, 302
        )
        self.client.force_login(self.operator)
        EmailAddress.objects.filter(user=self.operator).update(verified=False)
        self.assertRedirects(self.client.get(self.url(item)), reverse("account_email"))
        EmailAddress.objects.filter(user=self.operator).update(verified=True)
        self.operator.is_superuser = False
        self.operator.save(update_fields=["is_superuser"])
        self.assertEqual(
            self.client.post(self.url(item), {"confirm": "on"}).status_code, 403
        )
        self.operator.is_active = False
        self.operator.save(update_fields=["is_active"])
        self.assertEqual(
            self.client.post(self.url(item), {"confirm": "on"}).status_code, 302
        )
        self.assertTrue(type(item).objects.filter(pk=item.pk).exists())

    def test_csrf_and_allowed_methods(self):
        item = self.create_evidence()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        self.assertEqual(
            client.post(self.url(item), {"confirm": "on"}).status_code, 403
        )
        self.assertEqual(self.client.put(self.url(item)).status_code, 405)
        self.assertTrue(type(item).objects.filter(pk=item.pk).exists())

    def test_protected_import_source_shows_recovery_without_deleting(self):
        item = self.create_evidence(DocumentEvidence)
        VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=item.uploaded_by,
            document=item,
            timezone="UTC",
        )
        response = self.client.get(self.url(item))
        self.assertContains(response, "supports retained imported evidence")
        self.assertNotContains(response, "Permanently delete evidence")
        response = self.client.post(self.url(item), {"confirm": "on"})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(DocumentEvidence.objects.filter(pk=item.pk).exists())
        self.assertTrue(item.file.storage.exists(item.file.name))

    def test_wrong_case_attachment_is_not_disclosed_or_deleted(self):
        item = self.create_evidence(KINDS["phone-call"])
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=item,
            uploaded_by=self.editor,
            file=SimpleUploadedFile("foreign-case-original.wav", b"Synthetic audio"),
        )
        CallEvidenceAttachment.objects.filter(pk=attachment.pk).update(
            case=self.other_case
        )
        for method, status in ((self.client.get, 200), (self.client.post, 409)):
            response = method(self.url(item), {"confirm": "on"})
            self.assertContains(response, "inconsistent case links", status_code=status)
            self.assertNotContains(
                response, attachment.original_filename, status_code=status
            )
        self.assertTrue(
            CallEvidenceAttachment.objects.filter(pk=attachment.pk).exists()
        )
        self.assertTrue(attachment.file.storage.exists(attachment.file.name))

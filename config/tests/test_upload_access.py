"""Reject unauthorized file bodies before multipart parsing or temporary writes."""

from unittest.mock import patch
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import TemporaryFileUploadHandler
from django.test import Client, TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence


class UploadAccessTests(EvidenceViewTestMixin, TestCase):
    def client_for(self, role=None):
        client = Client(enforce_csrf_checks=True)
        if role:
            client.force_login(self.users[role])
        client.cookies[settings.CSRF_COOKIE_NAME] = "a" * 32
        return client

    def payload(self):
        return {
            "kind": "document",
            "title": "Synthetic upload",
            "submission_id": str(uuid4()),
            "csrfmiddlewaretoken": "a" * 32,
            "file": SimpleUploadedFile("source.txt", b"x" * (3 * 1024 * 1024)),
        }

    def test_account_and_case_gates_prevent_unauthorized_temporary_files(self):
        url = reverse("core:evidence:create", args=[self.case.pk])
        for role, status in ((None, 401), ("viewer", 403), ("outsider", 404)):
            with self.subTest(role=role):
                client = self.client_for(role)
                with patch.object(TemporaryFileUploadHandler, "new_file") as temporary:
                    response = client.post(
                        url, self.payload(), HTTP_ACCEPT="application/json"
                    )
                self.assertEqual(response.status_code, status)
                temporary.assert_not_called()
                self.assertIn("no-store", response["Cache-Control"])
        for state in ("unverified", "inactive", "revoked"):
            with self.subTest(state=state):
                client = self.client_for("editor")
                if state == "unverified":
                    EmailAddress.objects.filter(user=self.users["editor"]).update(
                        verified=False
                    )
                elif state == "inactive":
                    type(self.users["editor"]).objects.filter(
                        pk=self.users["editor"].pk
                    ).update(is_active=False)
                else:
                    CaseMembership.objects.filter(
                        user=self.users["editor"], case=self.case
                    ).delete()
                with patch.object(TemporaryFileUploadHandler, "new_file") as temporary:
                    response = client.post(
                        url, self.payload(), HTTP_ACCEPT="application/json"
                    )
                self.assertIn(response.status_code, (401, 403, 404))
                temporary.assert_not_called()
                EmailAddress.objects.filter(user=self.users["editor"]).update(
                    verified=True
                )
                type(self.users["editor"]).objects.filter(
                    pk=self.users["editor"].pk
                ).update(is_active=True)

    def test_anonymous_product_upload_is_rejected_before_multipart_parsing(self):
        with patch("django.http.multipartparser.MultiPartParser.parse") as parse:
            response = self.client_for().post(
                reverse("core:evidence:create", args=[self.case.pk]),
                self.payload(),
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(response.status_code, 401)
        parse.assert_not_called()

    def test_public_account_forms_refuse_file_parts_before_temporary_storage(self):
        for role in (None, "owner"):
            with self.subTest(role=role):
                client = self.client_for(role)
                with patch.object(TemporaryFileUploadHandler, "new_file") as temporary:
                    response = client.post(reverse("account_login"), self.payload())
                self.assertEqual(response.status_code, 403)
                temporary.assert_not_called()

    def test_authorized_uploads_keep_csrf_and_normal_file_storage(self):
        url = reverse("core:evidence:create", args=[self.case.pk])
        for role in ("owner", "editor"):
            client = self.client_for(role)
            data = self.payload()
            data["file"] = SimpleUploadedFile("source.txt", role.encode() * 600000)
            response = client.post(url, data, HTTP_ACCEPT="application/json")
            self.assertEqual(response.status_code, 201)
            self.assertTrue(
                DocumentEvidence.objects.filter(pk=response.json()["id"]).exists()
            )
        client = self.client_for("owner")
        data = self.payload()
        data.pop("csrfmiddlewaretoken")
        self.assertEqual(client.post(url, data).status_code, 403)

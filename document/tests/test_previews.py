"""Private original readers and bounded PDF rasterization."""

import hashlib
import io
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from PIL import Image

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.parsers import MAX_BYTES, run_parser
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from document.tests.test_analysis import pdf_bytes


class DocumentPreviewParserTests(SimpleTestCase):
    def test_requested_page_is_a_bounded_image(self):
        content = pdf_bytes("First page", "Second page")
        result = run_parser(
            "document_preview", content, options={"page": 2, "render": True}
        )
        import base64

        self.assertEqual(result["page_count"], 2)
        with Image.open(io.BytesIO(base64.b64decode(result["image"]))) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (400, 400))
            self.assertFalse(image.getexif())

    def test_encryption_page_bounds_and_bad_files(self):
        encrypted = run_parser("document_preview", pdf_bytes("Private", encrypted=True))
        self.assertEqual(encrypted["reason"], "encrypted_pdf")
        for content, options, code in (
            (pdf_bytes(None), {"page": 2}, "invalid_document_page"),
            (pdf_bytes(None), {"page": True}, "invalid_document_page"),
            (pdf_bytes(*[None] * 101), {}, "document_page_limit"),
            (b"%PDF-broken", {}, "malformed_input"),
        ):
            with self.subTest(code=code), self.assertRaises(AnalysisError) as raised:
                run_parser("document_preview", content, options=options)
            self.assertEqual(raised.exception.code, code)


class DocumentPreviewTests(EvidenceViewTestMixin, TestCase):
    def document(self, content=None):
        return DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic document",
            file=SimpleUploadedFile(
                "original.bin",
                content if content is not None else pdf_bytes("First", "Second"),
            ),
        )

    def page_url(self, item, page=1, case=None):
        return reverse(
            "core:evidence:document_page",
            kwargs={
                "case_id": (case or self.case).pk,
                "evidence_id": item.pk,
                "page": page,
            },
        )

    def test_current_members_read_archived_pdf_and_original_unchanged(self):
        item = self.document()
        original = item.file.read()
        self.login("owner")
        self.client.post(self.url("archive", item, kind="document"))
        for role in ("owner", "editor", "viewer"):
            self.login(role)
            response = self.client.get(self.page_url(item, 2))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "image/jpeg")
            self.assertIn("no-store", response["Cache-Control"])
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        detail = self.client.get(
            self.url("detail", item, kind="document"), {"document_page": 2}
        )
        self.assertContains(detail, "Page 2 of 2")
        self.assertContains(detail, "Previous page")
        self.assertNotContains(detail, "Next page")
        self.assertNotContains(detail, "<iframe")
        self.assertNotContains(detail, item.file.name)
        download = self.client.get(self.url("download", item, kind="document"))
        self.assertEqual(b"".join(download.streaming_content), original)
        item.refresh_from_db()
        self.assertEqual(item.sha256, hashlib.sha256(original).hexdigest())

    def test_utf8_is_escaped_exact_decoded_text(self):
        content = b'\xef\xbb\xbf  <script>alert("source")</script>\r\nend  '
        item = self.document(content)
        self.login("viewer")
        detail = self.client.get(self.url("detail", item, kind="document"))
        self.assertContains(detail, "Decoded UTF-8 display")
        self.assertContains(detail, "&lt;script&gt;")
        self.assertNotContains(detail, '<script>alert("source")')
        self.assertContains(detail, "end  ")
        download = self.client.get(self.url("download", item, kind="document"))
        self.assertEqual(b"".join(download.streaming_content), content)

    def test_authentication_revocation_foreign_case_and_write_rejection(self):
        item = self.document()
        url = self.page_url(item)
        self.assertEqual(self.client.get(url).status_code, 302)
        self.login("outsider")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login("viewer")
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(
            self.client.get(self.page_url(item, case=self.other_case)).status_code, 404
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login("owner")
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_unavailable_unsupported_and_changed_files_keep_download(self):
        self.login("owner")
        for content in (
            pdf_bytes("Encrypted", encrypted=True),
            b"\x00binary",
            b"%PDF-broken",
        ):
            item = self.document(content)
            detail = self.client.get(self.url("detail", item, kind="document"))
            self.assertContains(detail, self.url("download", item, kind="document"))
            self.assertEqual(self.client.get(self.page_url(item)).status_code, 422)
        item = self.document()
        DocumentEvidence.objects.filter(pk=item.pk).update(byte_size=MAX_BYTES + 1)
        with patch("document.previews.run_parser") as parser:
            self.assertEqual(self.client.get(self.page_url(item)).status_code, 422)
            parser.assert_not_called()
        DocumentEvidence.objects.filter(pk=item.pk).update(
            byte_size=item.byte_size, sha256="0" * 64
        )
        self.assertEqual(self.client.get(self.page_url(item)).status_code, 422)
        detail = self.client.get(self.url("detail", item, kind="document"))
        self.assertContains(detail, "check the retained original and storage")
        self.assertNotContains(detail, "The original download remains available")
        item.file.delete(save=False)
        self.assertEqual(self.client.get(self.page_url(item)).status_code, 422)
        detail = self.client.get(self.url("detail", item, kind="document"))
        self.assertContains(detail, "The original file is unavailable")
        self.assertContains(detail, "check the retained original and storage")
        self.assertNotContains(detail, "The original download remains available")

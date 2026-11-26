"""Retained .eml uploads, local extraction and access checks using synthetic mail."""

import hashlib
from email.message import EmailMessage
from email.policy import SMTP

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from cases.models import CaseMembership
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.errors import AnalysisError
from core.analysis.testing import file_source
from core.analysis.worker import process_run
from core.case_search.tests.fixtures import indexed
from core.models import CaseAnalysisBatch
from core.parsers import run_parser
from core.testing import EvidenceViewTestMixin
from document.analysis.deterministic import native_text
from document.models import DocumentEvidence


def email_message():
    message = EmailMessage()
    message["From"] = "Synthetic Sender <sender@example.test>"
    message["To"] = "reader@example.test"
    message["Date"] = "Mon, 23 Nov 2026 09:30:00 -0500"
    message["Subject"] = "Café receipt"
    message["Message-ID"] = "<sample@example.test>"
    return message


class EmailParserTests(SimpleTestCase):
    def test_prefers_plain_body_and_excludes_attachments_and_alternates(self):
        message = email_message()
        message.set_content("Synthetic pickup at the café", cte="base64")
        message.add_alternative("<p>Secondary rendering</p>", subtype="html")
        message.add_attachment("Private synthetic attachment", filename="notes.txt")
        original = message.as_bytes(policy=SMTP)
        source = file_source(original, original_filename="Saved.EML")

        result = native_text([source], {})

        self.assertEqual(result["outcome"], "partial")
        (finding,) = result["items"]
        for text in (
            "From: Synthetic Sender <sender@example.test>",
            "To: reader@example.test",
            "Date: Mon, 23 Nov 2026 09:30:00 -0500",
            "Subject: Café receipt",
            "Message-ID: <sample@example.test>",
            "Synthetic pickup at the café",
        ):
            self.assertIn(text, finding["text"])
        self.assertNotIn("Secondary rendering", finding["text"])
        self.assertNotIn("Private synthetic attachment", finding["text"])
        self.assertIn("MIME parts skipped", finding["values"]["Coverage"])
        self.assertEqual(finding["anchors"], [source.anchor()])
        self.assertEqual(finding["values"]["Input SHA-256"], source.item.sha256)
        self.assertEqual(source.item.file.storage.content, original)

    def test_html_only_returns_text_without_resources_or_script_contents(self):
        message = email_message()
        message.set_content(
            '<p>Visible &amp; &lt;literal&gt; <a href="https://example.test/private">link</a></p>'
            '<script>alert("hidden")</script><img src="https://example.test/remote">'
            "<style>.body { display: none }</style>",
            subtype="html",
        )
        parsed = run_parser(
            "document", message.as_bytes(policy=SMTP), options={"email": True}
        )
        self.assertTrue(parsed["partial"])
        self.assertEqual(parsed["body_format"], "Text extracted from HTML")
        self.assertIn("Visible & <literal> link", parsed["text"])
        for hidden in ("https://", "alert", "display: none"):
            self.assertNotIn(hidden, parsed["text"])

    def test_filename_only_attachment_is_excluded_from_body_selection(self):
        message = email_message()
        message.make_mixed()
        attachment = EmailMessage()
        attachment.set_content("Skipped text")
        attachment.set_param("name", "attachment.txt")
        body = EmailMessage()
        body.set_content("Readable body")
        message.attach(attachment)
        message.attach(body)
        result = run_parser(
            "document", message.as_bytes(policy=SMTP), options={"email": True}
        )
        self.assertTrue(result["partial"])
        self.assertIn("Readable body", result["text"])
        self.assertNotIn("Skipped text", result["text"])

    def test_missing_headers_excessive_headers_and_nested_parts(self):
        content = b"No headers here.\r\n\r\nJust synthetic text."
        parsed = run_parser("document", content, options={"email": True})
        self.assertEqual(parsed["reason"], "email_headers_missing")
        message = email_message()
        message.set_content("Leaf message")
        for _ in range(13):
            container = email_message()
            container.set_content("Outer message")
            container.add_attachment(message)
            message = container
        for content, expected in (
            (b"Subject: " + b"x" * (64 * 1024) + b"\r\n\r\n", "email_header_limit"),
            (message.as_bytes(policy=SMTP), "email_structure_limit"),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaises(AnalysisError) as raised,
            ):
                run_parser("document", content, options={"email": True})
            self.assertEqual(raised.exception.code, expected)


class EmailIntakeTests(EvidenceViewTestMixin, TestCase):
    def test_upload_processing_search_preview_and_download(self):
        message = email_message()
        message.set_content(
            'Synthetic blue door pickup. <script>alert("mail")</script>'
        )
        original = message.as_bytes(policy=SMTP)
        self.login("editor")
        response = self.client.post(
            self.url("create"),
            self.payload(
                "document",
                file=SimpleUploadedFile(
                    "saved.eml", original, content_type="message/rfc822"
                ),
            ),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 201)
        item = DocumentEvidence.objects.get(pk=response.json()["id"])
        self.assertEqual(item.original_filename, "saved.eml")
        self.assertEqual(item.sha256, hashlib.sha256(original).hexdigest())
        self.assertIsNone(item.collected_at)

        batch = CaseAnalysisBatch.objects.get(case=self.case)
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        run = batch.runs.get(operation="native_text")
        process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        self.assertFalse(run.external_processing)

        self.enterContext(
            indexed(
                self.case,
                [("passage", "", source.pk) for source in run.result.passages.all()],
            )
        )
        for role in ("owner", "viewer"):
            self.login(role)
            search = self.client.get(self.url("list"), {"q": "blue door pickup"})
            (row,) = search.context["rows"]
            self.assertEqual(row["id"], item.pk)
            self.assertIn("blue door pickup", row["content_match"]["excerpt"])
            url = self.url("detail", item, kind="document")
            preview = self.client.get(url)
            self.assertContains(preview, "Decoded email text")
            self.assertContains(preview, "&lt;script&gt;")
            self.assertNotContains(preview, '<script>alert("mail")</script>')
            download = self.client.get(self.url("download", item, kind="document"))
            self.assertEqual(b"".join(download.streaming_content), original)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_single_upload_uses_the_same_email_path(self):
        message = email_message()
        message.set_content("Synthetic body")
        self.login("owner")
        response = self.client.post(
            self.url("create"),
            self.payload(
                "document",
                file=SimpleUploadedFile("single.eml", message.as_bytes(policy=SMTP)),
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(response["Location"]), "Synthetic body")

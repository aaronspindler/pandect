"""Real parser subprocesses over generated, non-investigation file bytes."""

import io
import signal
import zlib
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
)

from core.analysis.errors import AnalysisError
from core.analysis.services import start_run
from core.analysis.tests.test_openai_gateway import OPENAI
from core.analysis.tests.testing import external_grant_kwargs, file_source
from core.analysis.worker import process_run
from core.models import GatewayRequest
from core.parsers import run_parser
from core.tests.testing import EvidenceViewTestMixin
from document.analysis.deterministic import native_text
from document.models import DocumentEvidence


def pdf_bytes(*pages, encrypted=False):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=200, height=200)
        if text is not None:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(
                b"BT /F1 12 Tf 10 100 Td (" + text.encode("ascii") + b") Tj ET"
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("synthetic-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def scanned_pdf(*, native_page=False):
    writer = PdfWriter()
    if native_page:
        writer.append(io.BytesIO(pdf_bytes("Synthetic native text")))
    with Image.new("RGB", (1000, 240), "white") as image:
        ImageDraw.Draw(image).text(
            (40, 60),
            "PANDECT LOCAL TEXT 12345",
            font=ImageFont.load_default(size=48),
            fill="black",
        )
        scan = io.BytesIO()
        image.save(scan, format="PDF")
    writer.append(scan)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class NativeDocumentTests(SimpleTestCase):
    def test_utf8_exact_derived_text_and_line_anchors_leave_original_unchanged(self):
        original = "\ufeff  Native café\r\nsecond line\n".encode()
        source = file_source(original)
        result = native_text([source], {})
        self.assertEqual(result["outcome"], "complete")
        item = result["items"][0]
        self.assertEqual(item["text"], "  Native café\r\nsecond line\n")
        self.assertEqual(item["anchors"], [source.anchor(line_start=1, line_end=2)])
        self.assertEqual(item["values"]["Input SHA-256"], source.item.sha256)
        self.assertTrue(item["values"]["UTF-8 BOM removed in derived text"])
        self.assertEqual(source.item.file.storage.content, original)

    def test_native_pdf_pages_and_mixed_page_coverage(self):
        original = pdf_bytes("Synthetic page one", None)
        source = file_source(original)
        result = native_text([source], {})
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["PDF pages"], 2)
        self.assertEqual(result["metrics"]["PDF pages without usable native text"], 1)
        self.assertEqual(result["items"][0]["text"].strip(), "Synthetic page one")
        self.assertEqual(result["items"][0]["anchors"], [source.anchor(page=1)])
        self.assertEqual(result["items"][1]["anchors"], [source.anchor(page=2)])
        self.assertIn("No native text", result["items"][1]["values"]["Coverage"])
        self.assertEqual(source.item.file.storage.content, original)

    def test_native_extraction_keeps_unread_scanned_pages_and_originals(self):
        original = scanned_pdf(native_page=True)
        source = file_source(original)
        with patch("core.analysis.gateway.GatewayClient.request") as gateway:
            result = native_text([source], {})
        gateway.assert_not_called()
        self.assertEqual(result["outcome"], "partial")
        native, scanned = result["items"]
        self.assertEqual(native["text"].strip(), "Synthetic native text")
        self.assertEqual(native["anchors"], [source.anchor(page=1)])
        self.assertEqual(scanned["text"], "")
        self.assertEqual(scanned["anchors"], [source.anchor(page=2)])
        self.assertEqual(scanned["values"]["Input SHA-256"], source.item.sha256)
        self.assertEqual(source.item.file.storage.content, original)

    def test_blank_encrypted_and_unknown_format_do_not_pass_as_extracted(self):
        result = native_text([file_source(pdf_bytes(None))], {})
        self.assertEqual(result["outcome"], "partial")
        self.assertFalse(result["items"][0]["text"])
        for content in (
            pdf_bytes("Secret", encrypted=True),
            b"PK\x03\x04\x00",
            b"\xff\xfe",
        ):
            with self.subTest(content=content[:20]):
                result = native_text([file_source(content)], {})
                self.assertEqual(result["outcome"], "unsupported")
                self.assertFalse(any(x["text"].strip() for x in result["items"]))
        self.assertEqual(native_text([file_source(b" \r\n")], {})["outcome"], "empty")

    def test_checksum_malformed_page_count_and_byte_limits(self):
        source = file_source(b"Retained original")
        source.item.file.storage.content = b"Replaced original"
        with self.assertRaises(AnalysisError) as changed:
            native_text([source], {})
        self.assertEqual(changed.exception.code, "source_changed")
        with self.assertRaises(AnalysisError):
            native_text([file_source(b"%PDF-corrupt")], {})
        with self.assertRaises(AnalysisError) as pages:
            native_text([file_source(pdf_bytes(*([None] * 101)))], {})
        self.assertEqual(pages.exception.code, "document_page_limit")
        with (
            override_settings(PANDECT_ANALYSIS_MAX_BYTES=3),
            self.assertRaises(AnalysisError) as size,
        ):
            native_text([file_source(b"1234")], {})
        self.assertEqual(size.exception.code, "input_limit")

    def test_parser_timeout_kills_and_reaps_child(self):
        import subprocess
        from unittest.mock import Mock

        process = Mock()
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("parser", 0.25),
            (b"", b""),
        ]
        process.poll.return_value = None
        with (
            patch("core.parsers.subprocess.Popen", return_value=process),
            patch("core.parsers.os.killpg") as kill_group,
            patch("core.parsers.time.monotonic", side_effect=[0, 21]),
            self.assertRaises(AnalysisError) as timeout,
        ):
            run_parser("document", b"text")
        self.assertEqual(timeout.exception.code, "parser_timeout")
        kill_group.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    def test_compressed_pdf_expansion_is_bounded_before_text_extraction(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        compressed = EncodedStreamObject()
        compressed._data = zlib.compress(b" " * (4 * 1024 * 1024 + 1))
        compressed[NameObject("/Filter")] = NameObject("/FlateDecode")
        page[NameObject("/Contents")] = writer._add_object(compressed)
        buffer = io.BytesIO()
        writer.write(buffer)
        with self.assertRaises(AnalysisError) as bounded:
            native_text([file_source(buffer.getvalue())], {})
        self.assertEqual(bounded.exception.code, "document_stream_limit")

    def test_parser_child_environment_excludes_deployment_secrets(self):
        from unittest.mock import Mock

        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0

        def start_child(*args, stdout, **kwargs):
            stdout.write(b'{"status":"complete"}')
            return process

        with (
            patch("core.parsers.os.killpg"),
            patch.dict("os.environ", {"SYNTHETIC_PROVIDER_KEY": "private-test-value"}),
            patch("core.parsers.subprocess.Popen", side_effect=start_child) as create,
        ):
            run_parser("document", b"text")
        self.assertNotIn("SYNTHETIC_PROVIDER_KEY", create.call_args.kwargs["env"])
        self.assertIn("-I", create.call_args.args[0])


@override_settings(PANDECT_AI_GATEWAY=OPENAI)
class ScannedUploadTests(EvidenceViewTestMixin, TestCase):
    def test_saved_scan_is_searchable_and_source_links_open_its_page(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic two-page original",
            file=SimpleUploadedFile("scan.pdf", scanned_pdf(native_page=True)),
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "ocr",
            [{"kind": "document", "id": str(document.pk)}],
            **external_grant_kwargs(self.case, "ocr"),
        )
        with patch(
            "core.analysis.gateway.GatewayClient.read_image",
            return_value=("SYNTHETIC SCAN", False),
        ):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        self.assertEqual(run.method_version, "7")
        self.assertFalse(GatewayRequest.objects.filter(run=run).exists())
        scanned = run.result.passages.get(text__contains="SYNTHETIC SCAN")
        self.assertEqual(scanned.evidence_id, document.pk)
        self.login("viewer")
        finding_url = reverse(
            "core:analysis:detail", kwargs={"case_id": self.case.pk, "run_id": run.pk}
        )
        finding = self.client.get(finding_url)
        original = self.url("detail", document, kind="document") + "?document_page=2"
        self.assertContains(finding, original + "#document-preview")
        self.assertContains(self.client.get(original), "Page 2 of 2")
        self.login("outsider")
        self.assertEqual(self.client.get(finding_url).status_code, 404)
        self.assertEqual(self.client.get(original).status_code, 404)

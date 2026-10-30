"""Real parser subprocesses over generated, non-investigation file bytes."""

import hashlib
import io
import signal
import zlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
)

from analysis.documents import native_text
from analysis.errors import AnalysisError
from analysis.parsers import run_parser
from analysis.sources import Source


class ByteStorage:
    def __init__(self, content):
        self.content = content

    def open(self, name, mode="rb"):
        return io.BytesIO(self.content)


def file_source(content, kind="document", **values):
    item = SimpleNamespace(
        pk=uuid4(),
        case_id=uuid4(),
        title="Synthetic retained source",
        file=SimpleNamespace(name="fixture", storage=ByteStorage(content)),
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        **values,
    )
    return Source(str(uuid4()), kind, item)


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
        self.assertIn("visual review", result["items"][1]["values"]["Coverage"])
        self.assertEqual(source.item.file.storage.content, original)

    def test_scanned_blank_encrypted_and_unknown_format_do_not_pass_as_extracted(self):
        for content in (
            pdf_bytes(None),
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
            patch("analysis.parsers.subprocess.Popen", return_value=process),
            patch("analysis.parsers.os.killpg") as kill_group,
            patch("analysis.parsers.time.monotonic", side_effect=[0, 21]),
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
        process.communicate.return_value = (b'{"status":"complete"}', b"")
        process.poll.return_value = 0
        process.returncode = 0
        with (
            patch("analysis.parsers.os.killpg"),
            patch.dict("os.environ", {"SYNTHETIC_PROVIDER_KEY": "private-test-value"}),
            patch("analysis.parsers.subprocess.Popen", return_value=process) as create,
        ):
            run_parser("document", b"text")
        self.assertNotIn("SYNTHETIC_PROVIDER_KEY", create.call_args.kwargs["env"])
        self.assertIn("-I", create.call_args.args[0])

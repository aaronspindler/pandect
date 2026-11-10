"""Real local recognition and bounded, private failure paths."""

import io
import os
import subprocess
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase
from PIL import Image, ImageDraw, ImageFont

from core.analysis.errors import AnalysisError
from core.analysis.testing import file_source, image_bytes, local_ocr_response
from core.parser_contract import MAX_TEXT, ParserFailure
from core.parsers import run_parser
from image.analysis.ai import ocr
from image.analysis.ai_parser import _ocr, _tesseract
from image.testing import jpeg_bytes


def printed_page():
    image = Image.new("RGB", (1000, 240), "white")
    ImageDraw.Draw(image).text(
        (40, 60),
        "PANDECT LOCAL TEXT 12345",
        font=ImageFont.load_default(size=48),
        fill="black",
    )
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class LocalOCRTests(SimpleTestCase):
    def test_iphone_jpeg_exports_use_local_ocr_and_report_partial_coverage(self):
        for content, expected in (
            (jpeg_bytes(size=(8064, 6048)), "complete"),
            (jpeg_bytes(multiple_pictures=True), "partial"),
        ):
            with self.subTest(expected=expected):
                source = file_source(content, "image")
                source.item.file.name = "iphone-export.jpeg"
                with patch("core.analysis.gateway.GatewayClient.request") as gateway:
                    result = ocr([source], {}, authorize=Mock())
                gateway.assert_not_called()
                self.assertEqual(result["outcome"], expected)
                self.assertIn(
                    "PANDECT LOCAL TEXT 12345",
                    " ".join(item["text"] for item in result["items"]),
                )
                if expected == "partial":
                    self.assertTrue(
                        any(
                            "primary image" in warning for warning in result["warnings"]
                        )
                    )
                self.assertEqual(source.item.file.storage.content, content)

    def test_real_engine_extracts_printed_text_without_network_or_credentials(self):
        content = printed_page()
        source = file_source(content, "image")
        with patch("core.analysis.gateway.GatewayClient.request") as gateway:
            result = ocr([source], {}, authorize=Mock())
        gateway.assert_not_called()
        self.assertIn(
            "PANDECT LOCAL TEXT 12345", " ".join(x["text"] for x in result["items"])
        )
        item = result["items"][0]
        self.assertIn("tesseract", item["values"]["OCR engine"])
        self.assertEqual(item["values"]["Input SHA-256"], source.item.sha256)
        self.assertEqual(
            item["anchors"][0]["coordinate_space"], "original_unoriented_pixels"
        )
        self.assertTrue(all(0 <= n <= 1 for n in item["anchors"][0]["region"]))
        self.assertEqual(source.item.file.storage.content, content)
        self.assertEqual(result["metrics"]["Recognized text regions"], 1)
        self.assertIn("Recognized 1", result["summary"])

    def test_no_recognized_text_does_not_claim_blank_image(self):
        stream = io.BytesIO()
        Image.new("RGB", (300, 200), "white").save(stream, format="PNG")
        result = ocr([file_source(stream.getvalue(), "image")], {}, authorize=Mock())
        self.assertEqual(result["outcome"], "partial")
        self.assertIn("No text recognized", result["summary"])
        self.assertEqual(result["metrics"]["Recognized text regions"], 0)
        self.assertEqual(result["metrics"]["Images with no recognized text"], 1)
        self.assertEqual(result["metrics"]["UTF-8 text bytes"], 0)
        self.assertIn(
            "No text recognized", result["items"][0]["values"]["Recognition status"]
        )

    def test_unrecognized_and_unsupported_inputs_have_distinct_coverage(self):
        source = file_source(image_bytes(), "image")
        reply = local_ocr_response(status="partial") | {"regions": []}
        with patch("image.analysis.ai.run_parser", return_value=reply):
            result = ocr(
                [source, file_source(b"%PDF-not-rendered")], {}, authorize=Mock()
            )
        self.assertEqual(result["outcome"], "partial")
        self.assertIn("No text recognized", result["summary"])
        self.assertEqual(result["metrics"]["Unsupported inputs"], 1)
        self.assertEqual(result["metrics"]["Images with no recognized text"], 1)
        self.assertEqual(result["metrics"]["Recognized text regions"], 0)
        self.assertTrue(all(not item["text"] for item in result["items"]))

    def test_unsupported_profiles_and_documents_never_dispatch(self):
        with patch("image.analysis.ai.run_parser") as parser:
            for parameters in (
                {"mode": "handwriting"},
                {"mode": "mixed"},
                {"mode": []},
                {"language": "fr"},
            ):
                result = ocr(
                    [file_source(image_bytes(), "image")], parameters, authorize=Mock()
                )
                self.assertEqual(result["outcome"], "unsupported")
            result = ocr([file_source(b"%PDF-not-rendered")], {}, authorize=Mock())
            self.assertEqual(result["outcome"], "unsupported")
            parser.assert_not_called()

    def test_revocation_before_or_after_recognition_blocks_results(self):
        reply = local_ocr_response()
        for steps, calls in (([PermissionDenied], 0), ([None, PermissionDenied], 1)):
            with patch("image.analysis.ai.run_parser", return_value=reply) as parser:
                with self.assertRaises(PermissionDenied):
                    ocr(
                        [file_source(image_bytes(), "image")],
                        {},
                        authorize=Mock(side_effect=steps),
                    )
                self.assertEqual(parser.call_count, calls)

    def test_missing_engine_is_safe_and_receives_no_operator_secrets(self):
        with patch.dict(
            os.environ,
            {"PATH": "/nonexistent", "PANDECT_AI_GATEWAY_TOKEN": "synthetic-secret"},
        ):
            with self.assertRaises(AnalysisError) as failure:
                run_parser("ocr", printed_page())
        self.assertEqual(failure.exception.code, "ocr_not_installed")

    def test_native_failures_timeouts_and_output_limits(self):
        for side_effect, code in (
            (FileNotFoundError(), "ocr_not_installed"),
            (subprocess.TimeoutExpired("tesseract", 15), "parser_timeout"),
        ):
            with patch(
                "image.analysis.ai_parser.subprocess.run", side_effect=side_effect
            ):
                with self.assertRaises(ParserFailure) as failure:
                    _tesseract(["--version"])
                self.assertEqual(str(failure.exception), code)
        with patch(
            "image.analysis.ai_parser.subprocess.run", return_value=Mock(returncode=1)
        ):
            with self.assertRaisesMessage(ParserFailure, "ocr_failed"):
                _tesseract(["--version"])

        def too_large(*args, **kwargs):
            kwargs["stdout"].write(b"a" * (MAX_TEXT + 1))
            return Mock(returncode=0)

        with patch("image.analysis.ai_parser.subprocess.run", side_effect=too_large):
            with self.assertRaisesMessage(ParserFailure, "extracted_text_limit"):
                _tesseract(["--version"])

    def test_regions_group_words_and_preserve_low_confidence(self):
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext\n"
        )
        tsv += "5\t1\t1\t1\t1\t1\t10\t10\t20\t20\t96\tFirst\n"
        tsv += "5\t1\t1\t1\t1\t2\t40\t10\t30\t20\t42\treading\n"
        with patch(
            "image.analysis.ai_parser._tesseract",
            side_effect=["tesseract fixture\n", tsv],
        ):
            result = _ocr(image_bytes())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["regions"][0]["text"], "First reading")
        self.assertEqual(result["regions"][0]["confidence"], 42)
        self.assertEqual(
            result["regions"][0]["bbox"], [10 / 120, 10 / 80, 70 / 120, 30 / 80]
        )
        for invalid in (
            "unexpected header\n",
            tsv.replace("\t96\t", "\tnan\t"),
            tsv.replace("\t10\t10\t", "\t9999\t10\t"),
        ):
            with patch(
                "image.analysis.ai_parser._tesseract",
                side_effect=["tesseract fixture\n", invalid],
            ):
                with self.assertRaisesMessage(ParserFailure, "ocr_invalid_output"):
                    _ocr(image_bytes())

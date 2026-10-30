"""Real local recognition and bounded, private failure paths."""

import io
import os
import subprocess
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase
from PIL import Image, ImageDraw, ImageFont

from analysis.errors import AnalysisError
from analysis.media import ocr
from analysis.parsers import MAX_TEXT, ParserFailure, _ocr, _tesseract, run_parser

from .test_documents import file_source
from .test_media import image_bytes
from .test_result_integrations import local_ocr_response


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
    def test_real_engine_extracts_printed_text_without_network_or_credentials(self):
        content = printed_page()
        source = file_source(content, "image")
        with patch("analysis.gateway.GatewayClient.request") as gateway:
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

    def test_no_recognized_text_does_not_claim_blank_image(self):
        stream = io.BytesIO()
        Image.new("RGB", (300, 200), "white").save(stream, format="PNG")
        result = ocr([file_source(stream.getvalue(), "image")], {}, authorize=Mock())
        self.assertEqual(result["outcome"], "partial")
        self.assertIn(
            "No text recognized", result["items"][0]["values"]["Recognition status"]
        )

    def test_unsupported_profiles_and_documents_never_dispatch(self):
        with patch("analysis.media.run_parser") as parser:
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
            with patch("analysis.media.run_parser", return_value=reply) as parser:
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
            with patch("analysis.parsers.subprocess.run", side_effect=side_effect):
                with self.assertRaises(ParserFailure) as failure:
                    _tesseract(["--version"])
                self.assertEqual(str(failure.exception), code)
        with patch("analysis.parsers.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaisesMessage(ParserFailure, "ocr_failed"):
                _tesseract(["--version"])

        def too_large(*args, **kwargs):
            kwargs["stdout"].write(b"a" * (MAX_TEXT + 1))
            return Mock(returncode=0)

        with patch("analysis.parsers.subprocess.run", side_effect=too_large):
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
            "analysis.parsers._tesseract", side_effect=["tesseract fixture\n", tsv]
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
                "analysis.parsers._tesseract",
                side_effect=["tesseract fixture\n", invalid],
            ):
                with self.assertRaisesMessage(ParserFailure, "ocr_invalid_output"):
                    _ocr(image_bytes())

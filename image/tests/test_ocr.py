"""Cloudflare vision extraction, image provenance and partial coverage."""

import base64
import hashlib
import io
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase
from PIL import Image

from core.analysis.errors import AnalysisError
from core.analysis.gateway import GatewayClient
from core.analysis.testing import file_source, image_bytes
from document.tests.test_analysis import scanned_pdf
from image.analysis.ai import ocr


class HostedOCRTests(SimpleTestCase):
    def recognize(
        self, sources, *, text="Synthetic visible text", partial=False, **options
    ):
        client = Mock()
        client.read_image.return_value = (text, partial)
        result = ocr(
            sources,
            {"_execution": {"model": "vision-test"}} | options,
            authorize=Mock(),
            client=client,
        )
        return result, client

    def test_only_metadata_free_derived_image_sent_with_original_checksum(self):
        original = image_bytes(exif=True)
        source = file_source(original, "image")
        result, client = self.recognize([source])
        (encoded,) = client.read_image.call_args.args
        raw = base64.b64decode(encoded)
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertFalse(image.getexif())
        (item,) = result["items"]
        self.assertEqual(item["text"], "Synthetic visible text")
        self.assertEqual(
            item["anchors"],
            [
                source.anchor(
                    region=[0, 0, 1, 1], coordinate_space="original_unoriented_pixels"
                )
            ],
        )
        self.assertEqual(
            item["values"]["Recognition JPEG SHA-256"], hashlib.sha256(raw).hexdigest()
        )
        self.assertEqual(
            item["values"]["Input SHA-256"], hashlib.sha256(original).hexdigest()
        )

    def test_scans_send_only_pages_without_native_text_and_keep_exact_page(self):
        source = file_source(scanned_pdf(native_page=True), "document")
        result, client = self.recognize([source], partial=True)
        client.read_image.assert_called_once()
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["items"][0]["anchors"], [source.anchor(page=2)])

    def test_missing_coverage_and_unsupported_files_are_disclosed(self):
        supported = file_source(image_bytes(), "image")
        unsupported = file_source(b"<svg/>", "image")
        with patch("image.analysis.ai.MAX_IMAGES_PER_RUN", 1):
            result, client = self.recognize(
                [supported, supported, unsupported], text=""
            )
        client.read_image.assert_called_once()
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["Images omitted"], 1)
        self.assertFalse(any(item["text"] for item in result["items"]))

    def test_invalid_profile_and_revocation_stop_before_transfer(self):
        source = file_source(image_bytes(), "image")
        result, client = self.recognize([source], mode="mixed")
        self.assertEqual(result["outcome"], "unsupported")
        client.read_image.assert_not_called()
        with self.assertRaises(PermissionDenied):
            ocr(
                [source],
                {},
                authorize=Mock(side_effect=PermissionDenied),
                client=client,
            )
        client.read_image.assert_not_called()

    def test_untrusted_model_output_is_validated(self):
        client = GatewayClient(None, Mock())
        with (
            patch.object(client, "request", return_value={}),
            patch.object(
                client, "_openai_text", return_value='{"text":"abc","partial":"yes"}'
            ),
        ):
            with self.assertRaisesMessage(AnalysisError, "gateway_invalid_response"):
                client.read_image("image")

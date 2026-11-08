"""Actual media decoding plus mocked, schema-checked inference responses."""

import base64
import io
import json
import random
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from PIL import Image, ImageDraw

from core.analysis.errors import AnalysisError
from core.analysis.testing import file_source, image_bytes
from image.analysis.ai import ocr
from image.analysis.metadata import image_metadata


class ImageAnalysisTests(SimpleTestCase):
    def test_image_metadata_keeps_raw_claims_and_ambiguous_time(self):
        source = file_source(image_bytes(exif=True), "image", width=999, height=None)
        result = image_metadata([source], {})
        decoded = result["items"][0]["values"]
        self.assertEqual(decoded["Decoded width"], 120)
        self.assertEqual(decoded["Supplied width"], 999)
        self.assertEqual(decoded["Capture time candidate"], "2026-10-27T12:34:56")
        self.assertEqual(decoded["Capture offset raw"], "Not supplied")
        self.assertEqual(decoded["Orientation claim"], 6)
        self.assertEqual(decoded["Input SHA-256"], source.item.sha256)
        self.assertIn("Synthetic camera", str(result["items"]))
        self.assertTrue(any("width disagree" in value for value in result["warnings"]))
        self.assertEqual(source.item.width, 999)

    def test_absent_and_malformed_xmp_metadata_are_explicit(self):
        absent = image_metadata([file_source(image_bytes(), "image")], {})
        self.assertEqual(
            absent["items"][0]["values"]["Embedded metadata"], "Absent or stripped"
        )
        for raw in ("<not-closed", '<!DOCTYPE x [<!ENTITY e "x">]><x>&e;</x>'):
            with self.subTest(raw=raw):
                result = image_metadata(
                    [file_source(image_bytes(xmp=raw), "image")], {}
                )
                self.assertIn(
                    raw, [json.loads(x["text"]) for x in result["items"] if x["text"]]
                )
                self.assertTrue(any("XMP" in warning for warning in result["warnings"]))
        with self.assertRaises(AnalysisError):
            image_metadata([file_source(b"\xff\xd8\xffbroken", "image")], {})

    def test_gps_candidates_preserve_raw_components_and_validate_ranges(self):
        image = Image.new("RGB", (20, 10), "white")
        for degree, expected in ((43, [43.0, -79.0]), (91, "Unknown")):
            tags = Image.Exif()
            tags[34853] = {1: "N", 2: (degree, 0, 0), 3: "W", 4: (79, 0, 0)}
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", exif=tags)
            result = image_metadata([file_source(buffer.getvalue(), "image")], {})
            self.assertEqual(result["items"][0]["values"]["GPS candidate"], expected)
            self.assertTrue(any("GPSLatitude" in x["text"] for x in result["items"]))

    def test_local_ocr_previews_use_recognized_pixels_and_keep_originals(self):
        from core.analysis.testing import local_ocr_response

        image = Image.new("RGBA", (2400, 1200), (255, 255, 255, 0))
        ImageDraw.Draw(image).rectangle((240, 240, 1920, 480), fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        source = file_source(buffer.getvalue(), "image")
        parsed = local_ocr_response(content=buffer.getvalue())
        authorize = Mock()
        with patch("image.analysis.ai.run_parser", return_value=parsed) as parser:
            result = ocr([source], {}, authorize=authorize)
        parser.assert_called_once_with("ocr", buffer.getvalue())
        self.assertEqual(authorize.call_count, 2)
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["data_base64"], parsed["image"])
        self.assertEqual((artifact["width"], artifact["height"]), (2048, 1024))
        self.assertEqual(
            result["items"][0]["anchors"][0]["region"], [0.1, 0.2, 0.8, 0.6]
        )
        with Image.open(
            io.BytesIO(base64.b64decode(artifact["data_base64"]))
        ) as preview:
            self.assertEqual(preview.size, (2048, 1024))
            self.assertFalse(preview.getexif())
            self.assertGreater(preview.getpixel((10, 10))[0], 245)
        self.assertEqual(source.item.file.storage.content, buffer.getvalue())

    def test_ocr_preview_limit_keeps_text_and_original_inspection_available(self):
        random_bytes = random.Random(7).randbytes(2048 * 2048 * 3)
        image = Image.frombytes("RGB", (2048, 2048), random_bytes)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        sources = [
            file_source(image_bytes(), "image"),
            file_source(buffer.getvalue(), "image"),
            file_source(image_bytes(), "image"),
        ]
        from core.analysis.testing import local_ocr_response

        replies = [
            local_ocr_response(content=s.item.file.storage.content) for s in sources
        ]
        with patch("image.analysis.ai.run_parser", side_effect=replies):
            result = ocr(sources, {}, authorize=Mock())
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertEqual(result["artifacts"][0]["source_id"], sources[0].id)
        self.assertLessEqual(
            sum(len(base64.b64decode(x["data_base64"])) for x in result["artifacts"]),
            2 * 1024 * 1024,
        )
        self.assertTrue(
            any(
                "2 MiB preview limit" in value and "originals" in value
                for value in result["warnings"]
            )
        )

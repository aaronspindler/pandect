"""Actual media decoding plus mocked, schema-checked inference responses."""

import io
import json

from django.test import SimpleTestCase
from PIL import Image

from core.analysis.errors import AnalysisError
from core.analysis.tests.testing import file_source, image_bytes
from image.analysis.metadata import image_metadata
from image.tests.testing import jpeg_bytes


class ImageAnalysisTests(SimpleTestCase):
    def test_48_megapixel_jpeg_metadata(self):
        content = jpeg_bytes(size=(8064, 6048))
        source = file_source(content, "image")
        source.item.original_filename = "iphone-export.jpeg"
        result = image_metadata([source], {})
        self.assertEqual(result["outcome"], "complete")
        values = result["items"][0]["values"]
        self.assertEqual(values["Decoded format"], "JPEG")
        self.assertEqual(
            (values["Decoded width"], values["Decoded height"]), (8064, 6048)
        )
        self.assertEqual(source.item.file.storage.content, content)

    def test_multi_picture_jpeg_metadata_discloses_primary_image_only(self):
        source = file_source(jpeg_bytes(multiple_pictures=True), "image")
        source.item.original_filename = "iphone-export.JPEG"
        result = image_metadata([source], {})
        self.assertEqual(result["outcome"], "partial")
        values = result["items"][0]["values"]
        self.assertEqual(values["Decoded format"], "MPO")
        self.assertEqual(values["Embedded images"], 2)
        self.assertEqual(values["Inspected image"], "Primary image (1 of 2)")
        self.assertTrue(any("primary image" in value for value in result["warnings"]))

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
        for raw in (
            "<not-closed",
            '<x value="broken\x00value"/>',
            '<!DOCTYPE x [<!ENTITY e "x">]><x>&e;</x>\x00',
        ):
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

    def test_nul_terminated_jpeg_xmp_is_interpreted_without_changing_raw_claims(self):
        packet = (
            '<?xpacket begin="\ufeff"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/" '
            'xmlns:claim="https://example.test/claims" claim:Software="Synthetic"/>'
            '   <?xpacket end="w"?>'
        )
        for padding in ("", "\x00", "\x00\x00\x00"):
            with self.subTest(padding=len(padding)):
                raw = packet + padding
                stream = io.BytesIO()
                with Image.new("RGB", (120, 80), "white") as image:
                    image.save(stream, format="JPEG", xmp=raw.encode())
                source = file_source(stream.getvalue(), "image")
                result = image_metadata([source], {})
                self.assertEqual(result["outcome"], "complete")
                self.assertFalse(any("XMP" in w for w in result["warnings"]))
                interpreted = next(
                    item["values"]
                    for item in result["items"]
                    if item["label"] == "Interpreted XMP attributes"
                )
                self.assertEqual(
                    interpreted["{https://example.test/claims}Software"], "Synthetic"
                )
                self.assertEqual(
                    interpreted["Trailing XMP NUL bytes ignored for interpretation"],
                    len(padding),
                )
                retained = next(
                    item
                    for item in result["items"]
                    if item["label"] == "Raw metadata · XMP"
                )
                self.assertEqual(json.loads(retained["text"]), raw)
                self.assertEqual(source.item.file.storage.content, stream.getvalue())

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

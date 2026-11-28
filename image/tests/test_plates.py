"""Local plate-region detection, exact recognition previews, and safe failures."""

import base64
import hashlib
import io
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase
from PIL import Image, ImageDraw

from core.analysis.errors import AnalysisError
from core.analysis.tests.testing import file_source
from image.analysis.plate_parser import MAX_CANDIDATES, candidate_quads, plate_likeness
from image.analysis.plate_parser import parse as parse_plates
from image.analysis.plates import plate_ocr as hosted_plate_ocr
from image.tests.testing import plate_scene


def plate_ocr(sources, parameters, *, authorize):
    client = Mock()
    client.read_image.return_value = ("TEST 482", False)
    return hosted_plate_ocr(
        sources,
        parameters | {"_execution": {"model": "test"}},
        authorize=authorize,
        client=client,
    )


class PlateTests(SimpleTestCase):
    def test_plate_score_filters_blank_panels_without_dropping_retained_regions(self):
        source = file_source(plate_scene(distractors=True), "image")
        result = plate_ocr([source], {}, authorize=Mock())
        self.assertEqual(result["metrics"]["Possible plate regions"], 5)
        self.assertEqual(result["metrics"]["Plate regions above filter"], 1)
        self.assertEqual(result["metrics"]["Weak plate regions"], 4)
        self.assertEqual(len(result["items"]), 5)
        strong = [
            item for item in result["items"] if not item["values"]["Weak plate region"]
        ]
        weak = [item for item in result["items"] if item["values"]["Weak plate region"]]
        self.assertEqual(len(strong), 1)
        self.assertEqual(len(weak), 4)
        readings = [item["text"] for item in strong]
        self.assertTrue(
            any("test 482" in text.casefold() for text in readings), readings
        )
        self.assertTrue(all(item["text"] == "" for item in weak))
        self.assertTrue(
            all(
                0 <= item["values"]["Plate-likeness score (0–100)"] <= 100
                for item in result["items"]
            )
        )
        self.assertTrue(
            all("Weak region OCR output" in item["values"] for item in weak)
        )
        self.assertTrue(
            all(
                "test 482" in item["values"]["Weak region OCR output"].casefold()
                for item in weak
            )
        )

    def test_confident_ocr_alone_cannot_make_a_blank_rectangle_a_likely_plate(self):
        stream = io.BytesIO()
        Image.new("L", (400, 150), "white").save(stream, format="JPEG")
        score = plate_likeness(
            {"aspect_ratio": 3, "rectangularity": 1},
            stream.getvalue(),
        )
        self.assertLess(score["plate_score"], 50)
        self.assertEqual(score["score_details"]["Character row points (0–60)"], 0)

    def test_real_local_detection_reads_straight_and_angled_synthetic_plate(self):
        for angle in (0, 8):
            with self.subTest(angle=angle):
                original = plate_scene(angle=angle)
                source = file_source(original, "image")
                authorize = Mock()
                with patch("core.analysis.gateway.GatewayClient.request") as gateway:
                    result = plate_ocr([source], {}, authorize=authorize)
                gateway.assert_not_called()
                self.assertEqual(authorize.call_count, 2)
                self.assertEqual(result["outcome"], "partial")
                self.assertEqual(result["metrics"]["Possible plate regions"], 1)
                self.assertTrue(
                    any("TEST 482" in item["text"] for item in result["items"])
                )
                self.assertEqual(len(result["items"]), 1)
                self.assertEqual(source.item.file.storage.content, original)
                for artifact in result["artifacts"]:
                    index = artifact.get("metadata", {}).get("finding_index")
                    if index is None:
                        continue
                    item = result["items"][index]
                    raw = base64.b64decode(artifact["data_base64"])
                    self.assertEqual(
                        hashlib.sha256(raw).hexdigest(),
                        item["values"]["Recognition JPEG SHA-256"],
                    )
                    self.assertEqual(
                        item["values"]["Input SHA-256"], source.item.sha256
                    )
                    self.assertEqual(
                        item["anchors"][0]["coordinate_space"],
                        "original_unoriented_pixels",
                    )
                    left, top, right, bottom = item["anchors"][0]["region"]
                    self.assertTrue(0 < left < right < 1 and 0 < top < bottom < 1)
                    self.assertLess(left * 2400, 1010)
                    self.assertGreater(right * 2400, 1400)
                    with Image.open(io.BytesIO(raw)) as crop:
                        self.assertFalse(crop.getexif())
                        self.assertEqual(
                            crop.size, (artifact["width"], artifact["height"])
                        )

    def test_blank_and_unsupported_images_are_not_proof_of_no_plate(self):
        stream = io.BytesIO()
        Image.new("RGB", (640, 480), "white").save(stream, format="PNG")
        result = plate_ocr(
            [file_source(stream.getvalue(), "image")], {}, authorize=Mock()
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["Possible plate regions"], 0)
        self.assertTrue(all(not item["text"] for item in result["items"]))
        result = plate_ocr([file_source(b"<svg/>", "image")], {}, authorize=Mock())
        self.assertEqual(result["outcome"], "unsupported")

    def test_candidates_are_bounded_and_nested_panels_are_not_repeated(self):
        with Image.new("RGB", (1600, 1000), "#303844") as image:
            draw = ImageDraw.Draw(image)
            for x in range(80, 1400, 300):
                for y in (100, 500):
                    draw.rectangle(
                        (x, y, x + 210, y + 90), fill="white", outline="black", width=4
                    )
            candidates, limited = candidate_quads(image)
        self.assertEqual(len(candidates), MAX_CANDIDATES)
        self.assertTrue(limited)
        self.assertEqual(len({tuple(x["bbox"]) for x in candidates}), MAX_CANDIDATES)

    def test_selection_and_current_permission_are_checked(self):
        source = file_source(plate_scene(), "image")
        for sources in ([], [source, source], [file_source(b"text", "document")]):
            with self.assertRaisesMessage(AnalysisError, "plate_image_required"):
                plate_ocr(sources, {}, authorize=Mock())
        for steps, expected_calls in (
            ([PermissionDenied], 0),
            ([None, PermissionDenied], 1),
        ):
            with patch("image.analysis.plates.run_parser", return_value={}) as parser:
                with self.assertRaises(PermissionDenied):
                    plate_ocr([source], {}, authorize=Mock(side_effect=steps))
                self.assertEqual(parser.call_count, expected_calls)

    def test_small_plate_inside_larger_panel_is_not_suppressed_as_duplicate(self):
        with Image.new("RGB", (1600, 1000), "#303844") as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle((300, 200, 1200, 650), fill="white")
            draw.rectangle((320, 220, 1180, 630), fill="#303844")
            draw.rectangle((600, 400, 840, 500), fill="white")
            candidates, _ = candidate_quads(image)
        self.assertTrue(
            any(
                abs(item["bbox"][0] * 1600 - 600) < 5
                and abs(item["bbox"][2] * 1600 - 840) < 5
                for item in candidates
            )
        )

    def test_preview_budget_does_not_drop_readings_or_provenance(self):
        source = file_source(plate_scene(), "image")
        with patch("image.analysis.plates.MAX_PREVIEW_BYTES", 1):
            result = plate_ocr([source], {}, authorize=Mock())
        self.assertEqual(result["artifacts"], [])
        self.assertTrue(any("TEST 482" in x["text"] for x in result["items"]))
        self.assertTrue(any("omitted" in x for x in result["warnings"]))

    def test_multi_picture_jpeg_uses_primary_image_and_discloses_omissions(self):
        from image.tests.testing import jpeg_bytes

        result = plate_ocr(
            [file_source(jpeg_bytes(multiple_pictures=True), "image")],
            {},
            authorize=Mock(),
        )
        self.assertEqual(result["outcome"], "partial")
        self.assertTrue(any("primary embedded image" in x for x in result["warnings"]))

    def test_missing_detector_has_an_operator_recovery_error(self):
        from core.parser_contract import ParserFailure

        with patch.dict("sys.modules", {"cv2": None}):
            with self.assertRaisesMessage(ParserFailure, "plate_detector_unavailable"):
                parse_plates("plate_ocr", b"", {})

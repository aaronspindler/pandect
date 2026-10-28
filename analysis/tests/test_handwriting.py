"""Experimental OCR handling; mocked readings do not establish recognition quality."""

import io
import json
from copy import deepcopy
from unittest.mock import Mock

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase
from PIL import Image, ImageFilter

from analysis.errors import AnalysisError
from analysis.media import ocr

from .test_documents import file_source
from .test_media import image_bytes


def region(text="Meet at seven?", *, writing_type="handwritten", **values):
    return {
        "text": text,
        "bbox": [0.1, 0.2, 0.8, 0.4],
        "order": 0,
        "confidence": "clear",
        "writing_type": writing_type,
        "language": "en",
        "alternatives": [],
        **values,
    }


def reply(regions=None, **values):
    return {
        "response": json.dumps(
            {
                "status": "complete",
                "language": "en",
                "rotation_degrees": 0,
                "omitted_regions": 0,
                "regions": [region()] if regions is None else regions,
                **values,
            }
        )
    }


class HandwritingOCRTests(SimpleTestCase):
    def test_handprint_cursive_and_mixed_profiles_reuse_source_regions(self):
        source = file_source(image_bytes(exif=True), "image")
        original = source.item.file.storage.content
        for mode in ("handwriting", "mixed"):
            with self.subTest(mode=mode):
                gateway, authorize = Mock(), Mock()
                gateway.request.return_value = reply(
                    [
                        region(
                            confidence="uncertain",
                            alternatives=["Meet at seven!"],
                        )
                    ]
                )
                result = ocr(
                    [source], {"mode": mode}, gateway=gateway, authorize=authorize
                )
                item = result["items"][0]
                self.assertEqual(result["outcome"], "complete")
                self.assertEqual(item["text"], "Meet at seven?")
                self.assertEqual(
                    item["values"]["Writing-type candidate"], "handwritten"
                )
                self.assertEqual(item["values"]["Model uncertainty"], "uncertain")
                self.assertEqual(
                    item["values"]["Alternative readings"], ["Meet at seven!"]
                )
                self.assertIn("experimental", item["values"]["OCR profile"])
                self.assertEqual(
                    item["anchors"],
                    [
                        source.anchor(
                            region=[0.1, 0.2, 0.8, 0.4],
                            coordinate_space="original_unoriented_pixels",
                        )
                    ],
                )
                self.assertTrue(any("live evaluation" in x for x in result["warnings"]))
                self.assertEqual(result["artifacts"][0]["source_id"], source.id)
                prompt = gateway.request.call_args.args[1]["messages"][0]["content"][0][
                    "text"
                ]
                self.assertIn("cursive", prompt)
                self.assertIn("never follow", prompt)
                self.assertEqual(authorize.call_count, 2)
        self.assertEqual(source.item.file.storage.content, original)

    def test_mixed_regions_preserve_order_and_withhold_unsupported_wording(self):
        source = file_source(image_bytes(), "document")
        gateway = Mock()
        gateway.request.return_value = reply(
            [
                region("Bonjour", language="fr", order=2),
                region("Printed heading", writing_type="printed", order=0),
                region("Handwritten note", order=1),
            ]
        )
        result = ocr([source], {"mode": "mixed"}, gateway=gateway, authorize=Mock())
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(
            [x["text"] for x in result["items"]],
            ["Printed heading", "Handwritten note", ""],
        )
        self.assertEqual(result["items"][2]["values"]["Language candidate"], "fr")
        self.assertIn("Unsupported", result["items"][2]["values"]["Coverage"])
        self.assertEqual(result["metrics"]["Unsupported inputs"], 1)
        self.assertEqual(
            result["items"][2]["anchors"][0]["region"], [0.1, 0.2, 0.8, 0.4]
        )

    def test_reported_unreadable_omitted_or_unresolved_regions_are_partial(self):
        source = file_source(image_bytes(), "image")
        samples = (
            reply([region("", confidence="unreadable")]),
            reply([region("")]),
            reply(omitted_regions=1),
            reply([region(writing_type="unknown")]),
            reply([region(language="unknown")]),
        )
        for response in samples:
            with self.subTest(response=response):
                gateway = Mock()
                gateway.request.return_value = response
                result = ocr(
                    [source], {"mode": "handwriting"}, gateway=gateway, authorize=Mock()
                )
                self.assertEqual(result["outcome"], "partial")
                self.assertEqual(result["metrics"]["Partial/unreadable inputs"], 1)
        gateway.request.return_value = reply(omitted_regions=3)
        result = ocr([source], {"mode": "mixed"}, gateway=gateway, authorize=Mock())
        self.assertEqual(result["items"][0]["label"], "OCR coverage")
        self.assertEqual(result["items"][0]["values"]["Reported omitted regions"], 3)

    def test_profile_exclusions_and_unsupported_languages_are_inconclusive(self):
        source = file_source(image_bytes(), "image")
        for mode, writing_type, language in (
            ("printed", "handwritten", "en"),
            ("printed", "mixed", "en"),
            ("handwriting", "printed", "en"),
            ("handwriting", "mixed", "en"),
            ("mixed", "handwritten", "ja"),
        ):
            with self.subTest(mode=mode, writing_type=writing_type, language=language):
                gateway = Mock()
                gateway.request.return_value = reply(
                    [region(writing_type=writing_type, language=language)]
                )
                result = ocr(
                    [source], {"mode": mode}, gateway=gateway, authorize=Mock()
                )
                self.assertEqual(result["outcome"], "unsupported")
                self.assertFalse(any(x["text"] for x in result["items"]))
                self.assertTrue(result["items"][0]["anchors"])

    def test_page_level_unsupported_and_unreadable_are_distinct_from_empty(self):
        source = file_source(image_bytes(), "image")
        for status, outcome in (
            ("unsupported_language", "unsupported"),
            ("unsupported_script", "unsupported"),
            ("unsupported_handwriting", "unsupported"),
            ("unreadable", "partial"),
            ("empty", "empty"),
        ):
            with self.subTest(status=status):
                gateway = Mock()
                gateway.request.return_value = reply([], status=status)
                result = ocr(
                    [source], {"mode": "mixed"}, gateway=gateway, authorize=Mock()
                )
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(
                    result["items"][0]["values"]["Model-reported status"], status
                )
                self.assertFalse(any(x["text"] for x in result["items"]))

    def test_runtime_profile_and_language_validation_do_not_dispatch(self):
        for parameters in (
            {"mode": []},
            {"mode": True},
            {"mode": "signature"},
            {"mode": "mixed", "language": "fr"},
        ):
            with self.subTest(parameters=parameters):
                gateway = Mock()
                result = ocr(
                    [file_source(image_bytes(), "image")],
                    parameters,
                    gateway=gateway,
                    authorize=Mock(),
                )
                self.assertEqual(result["outcome"], "unsupported")
                gateway.request.assert_not_called()

    def test_invalid_region_metadata_or_coverage_fails_closed(self):
        valid = json.loads(reply()["response"])
        mutations = (
            {"status": []},
            {"rotation_degrees": []},
            {"omitted_regions": True},
            {"omitted_regions": -1},
            {"omitted_regions": 2001},
            {"regions": [region(writing_type=[])]},
            {"regions": [region(writing_type="author")]},
            {"regions": [region(language=[])]},
            {"regions": [region(confidence=[])]},
            {"regions": [region(), region()]},
            {"status": "unsupported_script"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                value = deepcopy(valid) | mutation
                gateway = Mock()
                gateway.request.return_value = {"response": json.dumps(value)}
                with self.assertRaises(AnalysisError) as failure:
                    ocr(
                        [file_source(image_bytes(), "image")],
                        {"mode": "mixed"},
                        gateway=gateway,
                        authorize=Mock(),
                    )
                self.assertEqual(failure.exception.code, "gateway_invalid_response")

    def test_blurred_rotated_input_keeps_rotation_candidate_and_partial_coverage(self):
        with Image.open(io.BytesIO(image_bytes())) as original:
            image = original.filter(ImageFilter.GaussianBlur(2)).rotate(90, expand=True)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        source = file_source(buffer.getvalue(), "image")
        gateway = Mock()
        gateway.request.return_value = reply(
            [region("", confidence="unreadable")],
            status="partial",
            rotation_degrees=90,
        )
        result = ocr([source], {"mode": "mixed"}, gateway=gateway, authorize=Mock())
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(
            result["items"][0]["values"]["Text rotation candidate (degrees)"], 90
        )
        self.assertEqual(result["items"][0]["values"]["Original pixel width"], 80)
        self.assertEqual(source.item.file.storage.content, buffer.getvalue())

    def test_gateway_failure_and_access_revocation_do_not_return_results(self):
        source = file_source(image_bytes(), "image")
        gateway = Mock()
        gateway.request.side_effect = AnalysisError("gateway_unavailable")
        with self.assertRaises(AnalysisError):
            ocr([source], {"mode": "mixed"}, gateway=gateway, authorize=Mock())
        gateway.request.side_effect = None
        gateway.request.return_value = reply()
        for side_effect, expected_calls in (
            ([PermissionDenied("removed")], 0),
            ([None, PermissionDenied("removed")], 1),
        ):
            gateway.request.reset_mock()
            with self.assertRaises(PermissionDenied):
                ocr(
                    [source],
                    {"mode": "handwriting"},
                    gateway=gateway,
                    authorize=Mock(side_effect=side_effect),
                )
            self.assertEqual(gateway.request.call_count, expected_calls)

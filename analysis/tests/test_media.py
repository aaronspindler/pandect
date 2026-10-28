"""Actual media decoding plus mocked, schema-checked inference responses."""

import base64
import io
import json
import random
import struct
import wave
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase
from PIL import Image, ImageDraw, PngImagePlugin

from analysis.errors import AnalysisError
from analysis.media import audio_metadata, image_metadata, ocr, transcription

from .test_documents import file_source


def image_bytes(*, exif=False, xmp=None, rotate=False):
    image = Image.new("RGB", (120, 80), "white")
    ImageDraw.Draw(image).text((5, 20), "Synthetic receipt", fill="black")
    if rotate:
        image = image.rotate(90, expand=True)
    buffer = io.BytesIO()
    if exif:
        tags = Image.Exif()
        tags[271] = "Synthetic camera"
        tags[306] = "2026:10:27 12:34:56"
        tags[274] = 6
        image.save(buffer, format="JPEG", exif=tags)
    else:
        metadata = PngImagePlugin.PngInfo()
        if xmp is not None:
            metadata.add_itxt("XML:com.adobe.xmp", xmp)
        image.save(buffer, format="PNG", pnginfo=metadata)
    return buffer.getvalue()


def wav_bytes(seconds=1, value=200, *, channels=1, rate=8000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(struct.pack("<h", value) * int(rate * seconds) * channels)
    return buffer.getvalue()


def ocr_reply(
    *, text="Synthetic receipt", status="complete", confidence="clear", bbox=None
):
    return {
        "response": json.dumps(
            {
                "status": status,
                "language": "en",
                "rotation_degrees": 0,
                "regions": [
                    {
                        "text": text,
                        "bbox": bbox or [0.1, 0.2, 0.8, 0.4],
                        "order": 0,
                        "confidence": confidence,
                        "alternatives": ["Synthetic rece1pt"]
                        if confidence == "uncertain"
                        else [],
                    }
                ],
            }
        )
    }


class MediaExtractionTests(SimpleTestCase):
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

    def test_audio_metadata_unknown_supplied_values_and_quality_intervals(self):
        content = wav_bytes(value=0)
        source = file_source(
            content,
            "audio",
            duration=timedelta(seconds=2),
            sample_rate=None,
            channel_count=1,
        )
        result = audio_metadata([source], {})
        metadata = result["items"][0]["values"]
        self.assertEqual(metadata["Decoded duration (seconds)"], 1)
        self.assertEqual(metadata["Supplied sample_rate"], "Unknown")
        self.assertEqual(metadata["Audio-byte SHA-256"], source.item.sha256)
        self.assertEqual(source.item.duration.total_seconds(), 2)
        self.assertEqual(
            result["items"][1]["anchors"],
            [source.anchor(start_seconds=0, end_seconds=1)],
        )
        self.assertEqual(result["items"][1]["label"], "Digital-zero interval")
        clipped = audio_metadata([file_source(wav_bytes(value=32767), "audio")], {})
        self.assertIn("Near-full-scale", clipped["items"][1]["label"])
        with self.assertRaises(AnalysisError) as malformed:
            audio_metadata([file_source(content[:-20], "audio")], {})
        self.assertEqual(malformed.exception.code, "truncated_audio")

    def test_metadata_only_call_and_unsupported_audio_are_not_transcribed(self):
        gateway, authorize = Mock(), Mock()
        call = file_source(b"unused", "phone-call", duration=None)
        result = transcription([call], {}, gateway=gateway, authorize=authorize)
        self.assertEqual(result["outcome"], "unsupported")
        unsupported = transcription(
            [file_source(b"not a wav", "audio")],
            {},
            gateway=gateway,
            authorize=authorize,
        )
        self.assertEqual(unsupported["outcome"], "unsupported")
        gateway.request.assert_not_called()

    def test_printed_ocr_has_source_regions_reading_order_and_uncertain_alternatives(
        self,
    ):
        source = file_source(image_bytes(exif=True, rotate=True), "image")
        gateway, authorize = Mock(), Mock()
        gateway.request.return_value = ocr_reply(confidence="uncertain")
        result = ocr([source], {}, gateway=gateway, authorize=authorize)
        item = result["items"][0]
        self.assertEqual(item["text"], "Synthetic receipt")
        self.assertEqual(
            item["anchors"],
            [
                source.anchor(
                    region=[0.1, 0.2, 0.8, 0.4],
                    coordinate_space="original_unoriented_pixels",
                )
            ],
        )
        self.assertEqual(item["values"]["Model uncertainty"], "uncertain")
        self.assertEqual(item["values"]["Alternative readings"], ["Synthetic rece1pt"])
        self.assertEqual(item["values"]["Original pixel width"], 80)
        purpose, payload = gateway.request.call_args.args
        self.assertEqual(purpose, "vision")
        submitted = payload["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(submitted.startswith("data:image/jpeg;base64,"))
        with Image.open(
            io.BytesIO(base64.b64decode(submitted.split(",", 1)[1]))
        ) as prepared:
            self.assertFalse(prepared.getexif())
        self.assertEqual(authorize.call_count, 2)

    def test_ocr_unsupported_profile_pdf_and_empty_partial_outcomes(self):
        gateway, authorize = Mock(), Mock()
        source = file_source(image_bytes(), "image")
        self.assertEqual(
            ocr([source], {"mode": "unknown"}, gateway=gateway, authorize=authorize)[
                "outcome"
            ],
            "unsupported",
        )
        self.assertEqual(
            ocr(
                [file_source(b"%PDF-not-rendered")],
                {},
                gateway=gateway,
                authorize=authorize,
            )["outcome"],
            "unsupported",
        )
        gateway.request.assert_not_called()
        gateway.request.return_value = {
            "response": json.dumps(
                {"status": "empty", "language": "unknown", "regions": []}
            )
        }
        self.assertEqual(
            ocr([source], {}, gateway=gateway, authorize=authorize)["outcome"], "empty"
        )
        gateway.request.return_value = ocr_reply(
            status="partial", confidence="uncertain"
        )
        self.assertEqual(
            ocr([source], {}, gateway=gateway, authorize=authorize)["outcome"],
            "partial",
        )
        gateway.request.return_value = ocr_reply(bbox=[0, 0, 2, 1])
        with self.assertRaises(AnalysisError):
            ocr([source], {}, gateway=gateway, authorize=authorize)

    def test_ocr_preview_reuses_submitted_pixels_and_normalized_region_space(self):
        image = Image.new("RGBA", (2400, 1200), (255, 255, 255, 0))
        ImageDraw.Draw(image).rectangle((240, 240, 1920, 480), fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        source = file_source(buffer.getvalue(), "image")
        gateway, authorize = Mock(), Mock()
        gateway.request.return_value = ocr_reply()
        result = ocr([source], {}, gateway=gateway, authorize=authorize)
        artifact = result["artifacts"][0]
        submitted = gateway.request.call_args.args[1]["messages"][0]["content"][1][
            "image_url"
        ]["url"]
        self.assertEqual(artifact["data_base64"], submitted.split(",", 1)[1])
        self.assertEqual(artifact["source_id"], source.id)
        self.assertEqual(artifact["content_type"], "image/jpeg")
        self.assertEqual((artifact["width"], artifact["height"]), (2048, 1024))
        self.assertEqual(
            artifact["coordinate_space"],
            result["items"][0]["anchors"][0]["coordinate_space"],
        )
        self.assertEqual(
            result["items"][0]["anchors"][0]["region"], [0.1, 0.2, 0.8, 0.4]
        )
        with Image.open(
            io.BytesIO(base64.b64decode(artifact["data_base64"]))
        ) as preview:
            self.assertEqual(preview.size, (artifact["width"], artifact["height"]))
            self.assertLess(preview.getpixel((1024, 307))[0], 10)
            self.assertGreater(preview.getpixel((10, 10))[0], 245)
            self.assertFalse(preview.getexif())
        self.assertEqual(gateway.request.call_count, 1)

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
        gateway, authorize = Mock(), Mock()
        gateway.request.return_value = ocr_reply()
        result = ocr(sources, {}, gateway=gateway, authorize=authorize)
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
        self.assertEqual(gateway.request.call_count, 3)

    def test_long_recording_offsets_and_call_addition_attribution(self):
        source = file_source(wav_bytes(seconds=61), "audio")
        gateway, authorize = Mock(), Mock()
        gateway.request.side_effect = [
            {
                "text": text,
                "segments": [{"start": 0, "end": duration, "text": text}],
                "transcription_info": {"language": "en"},
            }
            for text, duration in (
                ("Café names", 30),
                ("overlapping speech candidate", 30),
                ("unclear", 1),
            )
        ]
        result = transcription([source], {}, gateway=gateway, authorize=authorize)
        self.assertEqual(
            [x["anchors"][0]["start_seconds"] for x in result["items"]], [0, 30, 60]
        )
        self.assertEqual(result["items"][-1]["anchors"][0]["end_seconds"], 61)
        self.assertEqual(gateway.request.call_count, 3)
        self.assertEqual(authorize.call_count, 6)
        addition = SimpleNamespace(**vars(source.item))
        addition.pk = uuid4()
        addition.audio_sha256 = source.item.sha256
        addition.sha256 = "different manifest checksum"
        addition.transcript = "Supplied transcript stays unchanged"
        call_source = replace(source, kind="facetime", attachment=addition)
        call_source.item.duration = timedelta(seconds=80)
        decoded = audio_metadata([call_source], {})
        self.assertEqual(
            decoded["items"][0]["values"]["Audio-byte SHA-256"], addition.audio_sha256
        )
        self.assertEqual(addition.transcript, "Supplied transcript stays unchanged")

    def test_untranscribed_intervals_remain_partial_and_provider_failure_aborts(self):
        source = file_source(wav_bytes(seconds=31), "audio")
        gateway, authorize = Mock(), Mock()
        gateway.request.side_effect = [
            {"text": "Middle", "segments": [{"start": 5, "end": 20, "text": "Middle"}]},
            {"text": "", "segments": []},
        ]
        result = transcription([source], {}, gateway=gateway, authorize=authorize)
        self.assertEqual(result["outcome"], "partial")
        gaps = [
            x
            for x in result["items"]
            if x["label"] == "Interval without transcribed text"
        ]
        self.assertEqual(len(gaps), 2)
        self.assertEqual(gaps[0]["anchors"][0]["start_seconds"], 0)
        self.assertEqual(gaps[1]["anchors"][0]["end_seconds"], 30)
        gateway.request.side_effect = [
            {
                "text": "Candidate",
                "segments": [{"start": 0, "end": 30, "text": "Candidate"}],
            },
            AnalysisError("gateway_unavailable"),
        ]
        with self.assertRaises(AnalysisError) as failed:
            transcription([source], {}, gateway=gateway, authorize=authorize)
        self.assertEqual(failed.exception.code, "gateway_unavailable")

    def test_silence_coarse_text_uncertainty_invalid_times_and_revocation(self):
        gateway, authorize = Mock(), Mock()
        silent = transcription(
            [file_source(wav_bytes(value=0), "audio")],
            {},
            gateway=gateway,
            authorize=authorize,
        )
        self.assertEqual(silent["outcome"], "empty")
        gateway.request.assert_not_called()
        source = file_source(wav_bytes(), "audio")
        gateway.request.return_value = {"text": "An uncertain name", "segments": []}
        coarse = transcription([source], {}, gateway=gateway, authorize=authorize)
        self.assertEqual(coarse["outcome"], "partial")
        gateway.request.return_value = {
            "text": "Name",
            "segments": [{"start": 0, "end": 1, "text": "Name", "avg_logprob": -2}],
        }
        uncertain = transcription([source], {}, gateway=gateway, authorize=authorize)
        self.assertEqual(
            uncertain["items"][0]["values"]["Model uncertainty"],
            "Review uncertain speech",
        )
        gateway.request.return_value = {
            "text": "bad",
            "segments": [{"start": 0, "end": 9, "text": "bad"}],
        }
        with self.assertRaises(AnalysisError):
            transcription([source], {}, gateway=gateway, authorize=authorize)
        authorize.side_effect = PermissionDenied("Access removed")
        gateway.request.reset_mock()
        with self.assertRaises(PermissionDenied):
            transcription([source], {}, gateway=gateway, authorize=authorize)
        gateway.request.assert_not_called()

"""Actual AAC-in-M4A decoding with bounded, attributed transcription chunks."""

import base64
import io
import wave
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from audio import parsers
from audio.analysis.ai import transcription
from audio.analysis.metadata import audio_metadata
from audio.testing import m4a_bytes
from core.analysis.errors import AnalysisError
from core.analysis.testing import file_source
from core.parser_contract import ParserFailure
from core.parsers import run_parser


class M4AAudioTests(SimpleTestCase):
    def test_metadata_decodes_stereo_aac_and_preserves_original_attribution(self):
        content = m4a_bytes(channels=2, rate=44100)
        source = file_source(content, "audio")
        parsed = run_parser("audio", content)
        self.assertEqual(parsed["status"], "complete")
        self.assertEqual(parsed["format"], "M4A")
        self.assertIn("AAC", parsed["codec"])
        self.assertIn("PyAV", parsed["version"])
        self.assertEqual(parsed["sample_rate"], 44100)
        self.assertEqual(parsed["channels"], 2)
        self.assertAlmostEqual(parsed["duration_seconds"], 1, places=3)
        result = audio_metadata([source], {})
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(
            result["items"][0]["values"]["Audio-byte SHA-256"], source.item.sha256
        )
        self.assertEqual(source.item.file.storage.content, content)

    def test_transcription_sends_pcm_chunks_with_original_timeline_and_hash(self):
        content = m4a_bytes(seconds=31)
        source = file_source(content, "audio")
        client = Mock()
        durations = []

        def response(operation, payload):
            self.assertEqual(operation, "transcription")
            with wave.open(io.BytesIO(base64.b64decode(payload["audio"])), "rb") as wav:
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getframerate(), 8000)
                duration = wav.getnframes() / wav.getframerate()
            durations.append(duration)
            return {
                "text": "Synthetic candidate",
                "segments": [
                    {"start": 0, "end": duration, "text": "Synthetic candidate"}
                ],
            }

        client.request.side_effect = response
        result = transcription([source], {}, client=client)
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(durations, [30, 1])
        self.assertEqual(
            [item["anchors"][0]["start_seconds"] for item in result["items"]],
            [0, 30],
        )
        self.assertEqual(result["items"][-1]["anchors"][0]["end_seconds"], 31)
        for item in result["items"]:
            self.assertEqual(item["values"]["Audio-byte SHA-256"], source.item.sha256)
        self.assertEqual(source.item.file.storage.content, content)

    def test_corrupt_m4a_is_rejected_before_inference(self):
        content = m4a_bytes()
        client = Mock()
        with self.assertRaises(AnalysisError):
            transcription(
                [file_source(content[: content.index(b"moov") - 4], "audio")],
                {},
                client=client,
            )
        client.request.assert_not_called()

    def test_multiple_audio_tracks_are_rejected_instead_of_selecting_one(self):
        parsed = run_parser("audio", m4a_bytes(tracks=2))
        self.assertEqual(parsed["status"], "unsupported")
        self.assertEqual(parsed["reason"], "unsupported_audio_encoding")

    def test_decoded_byte_and_duration_limits_apply_to_compressed_audio(self):
        content = m4a_bytes()
        self.assertLess(len(content), 8000 * 2)
        with (
            patch.object(parsers, "MAX_BYTES", len(content) + 1),
            self.assertRaisesMessage(ParserFailure, "input_limit"),
        ):
            parsers.parse("audio", content, {})
        with (
            patch.object(parsers, "MAX_AUDIO_SECONDS", 0.5),
            self.assertRaisesMessage(ParserFailure, "audio_duration_limit"),
        ):
            parsers.parse("audio", content, {})

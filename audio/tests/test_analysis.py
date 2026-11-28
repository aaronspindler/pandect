"""Actual media decoding plus mocked, schema-checked inference responses."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from django.test import SimpleTestCase

from audio.analysis.ai import transcription
from audio.analysis.metadata import audio_metadata
from core.analysis.errors import AnalysisError
from core.analysis.tests.testing import file_source, wav_bytes


class AudioAnalysisTests(SimpleTestCase):
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
        client = Mock()
        call = file_source(b"unused", "phone-call", duration=None)
        result = transcription([call], {}, client=client)
        self.assertEqual(result["outcome"], "unsupported")
        unsupported = transcription(
            [file_source(b"not a wav", "audio")],
            {},
            client=client,
        )
        self.assertEqual(unsupported["outcome"], "unsupported")
        client.request.assert_not_called()

    def test_long_recording_offsets_and_call_addition_attribution(self):
        source = file_source(wav_bytes(seconds=61), "audio")
        client = Mock()
        client.request.side_effect = [
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
        result = transcription([source], {}, client=client)
        self.assertEqual(
            [x["anchors"][0]["start_seconds"] for x in result["items"]], [0, 30, 60]
        )
        self.assertEqual(result["items"][-1]["anchors"][0]["end_seconds"], 61)
        self.assertEqual(client.request.call_count, 3)
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
        client = Mock()
        client.request.side_effect = [
            {"text": "Middle", "segments": [{"start": 5, "end": 20, "text": "Middle"}]},
            {"text": "", "segments": []},
        ]
        result = transcription([source], {}, client=client)
        self.assertEqual(result["outcome"], "partial")
        gaps = [
            x
            for x in result["items"]
            if x["label"] == "Interval without transcribed text"
        ]
        self.assertEqual(len(gaps), 2)
        self.assertEqual(gaps[0]["anchors"][0]["start_seconds"], 0)
        self.assertEqual(gaps[1]["anchors"][0]["end_seconds"], 30)
        client.request.side_effect = [
            {
                "text": "Candidate",
                "segments": [{"start": 0, "end": 30, "text": "Candidate"}],
            },
            AnalysisError("gateway_unavailable"),
        ]
        with self.assertRaises(AnalysisError) as failed:
            transcription([source], {}, client=client)
        self.assertEqual(failed.exception.code, "gateway_unavailable")

    def test_silence_coarse_text_uncertainty_and_invalid_times(self):
        client = Mock()
        silent = transcription(
            [file_source(wav_bytes(value=0), "audio")],
            {},
            client=client,
        )
        self.assertEqual(silent["outcome"], "empty")
        client.request.assert_not_called()
        source = file_source(wav_bytes(), "audio")
        client.request.return_value = {"text": "An uncertain name", "segments": []}
        coarse = transcription([source], {}, client=client)
        self.assertEqual(coarse["outcome"], "partial")
        client.request.return_value = {
            "text": "Name",
            "segments": [{"start": 0, "end": 1, "text": "Name", "avg_logprob": -2}],
        }
        uncertain = transcription([source], {}, client=client)
        self.assertEqual(
            uncertain["items"][0]["values"]["Model uncertainty"],
            "Review uncertain speech",
        )
        client.request.return_value = {
            "text": "bad",
            "segments": [{"start": 0, "end": 9, "text": "bad"}],
        }
        with self.assertRaises(AnalysisError):
            transcription([source], {}, client=client)

    def test_model_timestamp_outside_submitted_audio_is_rejected(self):
        source = file_source(wav_bytes(seconds=0.9975), "audio")
        client = Mock()
        client.request.return_value = {
            "text": "Candidate speech",
            "segments": [{"start": 0, "end": 1, "text": "Candidate speech"}],
        }
        with self.assertRaises(AnalysisError):
            transcription([source], {"backend": "gateway"}, client=client)

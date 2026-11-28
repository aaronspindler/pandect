"""Speech gateway bodies carry only the prepared chunk and keep real timestamps."""

import json
from base64 import b64encode
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

from django.test import SimpleTestCase

from audio.analysis.ai import _segments, transcription
from core.analysis.errors import AnalysisError
from core.analysis.gateway_audio import build_audio_request, decode_audio_response
from core.analysis.tests.testing import file_source, wav_bytes


class GatewayAudioTests(SimpleTestCase):
    def test_supported_audio_transports_send_only_the_prepared_wav(self):
        wav = wav_bytes()
        encoded = b64encode(wav).decode()
        payload = {"audio": encoded, "language": "en", "filename": "private-original"}
        execution = {"max_request_bytes": 65536, "model": "gpt-transcribe"}
        body, content_type = build_audio_request(
            execution | {"api": "audio-transcriptions"}, payload
        )
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + body
        )
        parts = list(message.iter_parts())
        self.assertEqual(
            [p.get_param("name", header="Content-Disposition") for p in parts],
            ["model", "languages[]", "file"],
        )
        self.assertEqual(parts[-1].get_filename(), "chunk.wav")
        self.assertEqual(parts[-1].get_payload(decode=True), wav)
        self.assertNotIn(b"private-original", body)
        self.assertEqual(
            build_audio_request(execution | {"api": "nova-3"}, payload),
            (wav, "audio/wav"),
        )
        for model in (
            "elevenlabs/scribe-v3",
            "xai/grok-stt",
            "openai/gpt-4o-transcribe",
        ):
            with self.subTest(model=model):
                body, content_type = build_audio_request(
                    execution | {"api": "audio-run", "catalog_model": model}, payload
                )
                self.assertEqual(content_type, "application/json")
                request = json.loads(body)
                self.assertEqual(request["model"], model)
                self.assertEqual(
                    request["input"]["file"], f"data:audio/wav;base64,{encoded}"
                )
                self.assertNotIn(b"private-original", body)

    def test_normalized_words_keep_times_and_text_only_results_keep_chunk_anchor(self):
        word = {
            "word": "sample",
            "punctuated_word": "Sample.",
            "start": 0.25,
            "end": 0.75,
        }
        nova = {
            "results": {
                "channels": [
                    {"alternatives": [{"transcript": "Sample.", "words": [word]}]}
                ]
            }
        }
        decoded = decode_audio_response({"api": "nova-3"}, nova)
        self.assertEqual(
            decoded["segments"], [{"text": "Sample.", "start": 0.25, "end": 0.75}]
        )
        client = Mock()
        client.request.side_effect = [
            decoded,
            decode_audio_response(
                {"api": "audio-transcriptions"}, {"text": "Last second."}
            ),
        ]
        source = file_source(wav_bytes(seconds=31), "audio")
        result = transcription([source], {"backend": "gateway"}, client=client)
        self.assertEqual(result["outcome"], "partial")
        candidates = [item for item in result["items"] if item.get("text")]
        self.assertEqual(
            candidates[0]["anchors"],
            [source.anchor(start_seconds=0.25, end_seconds=0.75)],
        )
        self.assertEqual(
            candidates[1]["anchors"], [source.anchor(start_seconds=30, end_seconds=31)]
        )

    def test_rejects_invalid_wire_data_and_malformed_replies(self):
        execution = {"api": "nova-3", "max_request_bytes": 65536}
        for value in ("bad!base64", "https://example.invalid/recording", "eA=="):
            with (
                self.subTest(value=value),
                self.assertRaisesMessage(AnalysisError, "gateway_invalid_input"),
            ):
                build_audio_request(execution, {"audio": value, "language": "en"})
        with self.assertRaisesMessage(AnalysisError, "gateway_input_limit"):
            build_audio_request(
                execution | {"max_request_bytes": 2},
                {"audio": "X" * 8, "language": "en"},
            )
        for result in (
            {},
            {"results": {"channels": []}},
            {"results": {"channels": [{"alternatives": []}]}},
        ):
            with (
                self.subTest(result=result),
                self.assertRaisesMessage(AnalysisError, "gateway_invalid_response"),
            ):
                decode_audio_response(execution, result)

    def test_word_timings_form_readable_phrases_after_validation(self):
        execution = {"api": "audio-run"}
        words = [
            {"text": token, "start": start, "end": end}
            for token, start, end in (
                ("First", 0.0, 0.4),
                ("phrase.", 0.5, 0.9),
                ("Next", 1.0, 1.4),
                ("pause", 3.0, 3.5),
            )
        ]
        result = {"text": "First phrase. Next pause", "words": words}
        _, phrases = _segments(decode_audio_response(execution, result), 4)
        self.assertEqual(
            phrases,
            [
                {"text": "First phrase.", "start": 0, "end": 0.9},
                {"text": "Next", "start": 1, "end": 1.4},
                {"text": "pause", "start": 3, "end": 3.5},
            ],
        )
        supplied = [{"text": result["text"], "start": 0.0, "end": 3.5}]
        _, phrases = _segments(
            decode_audio_response(execution, result | {"segments": supplied}), 4
        )
        self.assertEqual(phrases, supplied)
        # A bad inner word must not disappear inside an otherwise valid phrase.
        words[1]["end"] = 40
        with self.assertRaisesMessage(AnalysisError, "gateway_invalid_response"):
            _segments(decode_audio_response(execution, result), 4)

    def test_continuous_word_timings_stay_bounded_over_a_full_recording(self):
        words = [
            {"text": "word", "start": i * 0.6, "end": i * 0.6 + 0.4} for i in range(50)
        ]
        result = {"text": " ".join(w["text"] for w in words), "words": words}
        _, phrases = _segments(decode_audio_response({"api": "audio-run"}, result), 30)
        # Sixty 30-second chunks, with at most one gap per phrase.
        self.assertLess(60 * (2 * len(phrases) + 1), 5000)
        self.assertEqual(" ".join(phrase["text"] for phrase in phrases), result["text"])

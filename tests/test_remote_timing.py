import pytest

from pandect.processing.remote.timing import (
    TranscriptTimingError,
    validate_chunk_transcription_timing,
    validate_followup_transcript_references,
)


def _transcription(*, text="hello", start=0.1, end=0.9, channel=0):
    word = {
        "word": text,
        "start": start,
        "end": end,
        "confidence": 0.9,
        "speaker": 0,
    }
    return {
        "channels": [
            {
                "alternatives": [
                    {
                        "transcript": text,
                        "confidence": 0.9,
                        "words": [word] if text else [],
                    }
                ]
            }
        ],
        "utterances": (
            [
                {
                    "transcript": text,
                    "start": start,
                    "end": end,
                    "confidence": 0.9,
                    "channel": channel,
                    "speaker": 0,
                    "words": [word],
                }
            ]
            if text
            else []
        ),
    }


def test_chunk_timing_accepts_bounded_speech_and_explicit_no_speech():
    validate_chunk_transcription_timing(_transcription(), duration_seconds="1.0")
    validate_chunk_transcription_timing(_transcription(text=""), duration_seconds=1)


@pytest.mark.parametrize(
    "payload,duration",
    [
        (_transcription(end=2), 1),
        (_transcription(channel=1), 1),
        (_transcription(start=0.8, end=0.7), 1),
    ],
)
def test_chunk_timing_rejects_impossible_source_references(payload, duration):
    with pytest.raises(TranscriptTimingError):
        validate_chunk_transcription_timing(payload, duration_seconds=duration)


def test_chunk_timing_requires_timed_evidence_for_nonblank_speech():
    value = _transcription()
    value["channels"][0]["alternatives"][0]["words"] = []
    with pytest.raises(TranscriptTimingError):
        validate_chunk_transcription_timing(value, duration_seconds=1)


def _structured_source():
    record = {
        "start": 0.1,
        "end": 1.2,
        "chunk_speaker": 0,
        "speaker_scope": "chunk_local",
    }
    return {
        "transcript": {
            "streams": [
                {
                    "stream_index": 0,
                    "words": [record],
                    "utterances": [record],
                }
            ]
        }
    }


def _analysis(**turn_overrides):
    turn = {
        "speaker_label": "chunk speaker 0",
        "stream_index": 0,
        "start_seconds": 0.1,
        "end_seconds": 1.2,
        **turn_overrides,
    }
    return {
        "speaker_turn_summaries": [turn],
        "important_timestamps": [
            {"stream_index": 0, "start_seconds": 0.5, "end_seconds": 0.5}
        ],
    }


def test_followup_references_must_match_exact_stream_time_and_speaker():
    validate_followup_transcript_references(
        _analysis(), structured_source=_structured_source()
    )
    for overrides in (
        {"stream_index": 4},
        {"speaker_label": "chunk speaker 9"},
        {"start_seconds": 2, "end_seconds": 3},
    ):
        with pytest.raises(TranscriptTimingError):
            validate_followup_transcript_references(
                _analysis(**overrides), structured_source=_structured_source()
            )

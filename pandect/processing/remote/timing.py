"""Source-bound validation for remote transcript timing hypotheses."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

TIMING_TOLERANCE_SECONDS = 0.05


class TranscriptTimingError(ValueError):
    """A remote response cites timing that the consented source cannot support."""


def _number(value: Any) -> float | None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        return None
    return float(value)


def _duration_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return float(parsed)


def _timed_records(records: Any) -> list[Mapping[str, Any]]:
    if not isinstance(records, list) or any(
        not isinstance(record, Mapping) for record in records
    ):
        raise TranscriptTimingError("Transcript timing records are invalid.")
    return records


def _validate_ordered_records(
    records: Iterable[Mapping[str, Any]],
    *,
    duration_seconds: float,
    tolerance_seconds: float,
) -> None:
    previous_start = 0.0
    for record in records:
        start = _number(record.get("start"))
        end = _number(record.get("end"))
        if (
            start is None
            or end is None
            or start < 0
            or end < start
            or end > duration_seconds + tolerance_seconds
            or start + tolerance_seconds < previous_start
        ):
            raise TranscriptTimingError(
                "Transcript timing falls outside the consented audio chunk."
            )
        previous_start = start


def validate_chunk_transcription_timing(
    transcript: Mapping[str, Any],
    *,
    duration_seconds: Any,
    allowed_channels: frozenset[int] = frozenset({0}),
    tolerance_seconds: float = TIMING_TOLERANCE_SECONDS,
) -> None:
    """Validate provider-local times against one exact normalized audio chunk.

    The small tolerance only accommodates provider rounding at the encoded chunk
    boundary. It does not permit references into overlap-adjacent source media.
    """
    duration = _duration_number(duration_seconds)
    if duration is None or duration < 0 or tolerance_seconds < 0:
        raise TranscriptTimingError("The consented audio duration is invalid.")
    channels = transcript.get("channels")
    utterances = _timed_records(transcript.get("utterances"))
    if (
        not isinstance(channels, list)
        or not channels
        or any(not isinstance(channel, Mapping) for channel in channels)
    ):
        raise TranscriptTimingError("Transcript channels are invalid.")

    has_speech = False
    for channel in channels:
        alternatives = channel.get("alternatives")
        if (
            not isinstance(alternatives, list)
            or not alternatives
            or any(not isinstance(alternative, Mapping) for alternative in alternatives)
        ):
            raise TranscriptTimingError("Transcript alternatives are invalid.")
        for alternative in alternatives:
            text = alternative.get("transcript")
            words = _timed_records(alternative.get("words"))
            if not isinstance(text, str):
                raise TranscriptTimingError("Transcript text is invalid.")
            _validate_ordered_records(
                words,
                duration_seconds=duration,
                tolerance_seconds=tolerance_seconds,
            )
            if text.strip():
                has_speech = True
                if not words:
                    raise TranscriptTimingError(
                        "A nonblank transcript requires source-timed words."
                    )

    _validate_ordered_records(
        utterances,
        duration_seconds=duration,
        tolerance_seconds=tolerance_seconds,
    )
    if has_speech and not utterances:
        raise TranscriptTimingError(
            "A nonblank transcript requires source-timed utterances."
        )
    if not has_speech and utterances:
        raise TranscriptTimingError(
            "A no-speech transcript cannot contain timed utterances."
        )

    for utterance in utterances:
        channel = utterance.get("channel")
        if (
            not isinstance(channel, int)
            or isinstance(channel, bool)
            or channel not in allowed_channels
        ):
            raise TranscriptTimingError(
                "An utterance cites a channel outside the consented mono derivative."
            )
        utterance_start = float(utterance["start"])
        utterance_end = float(utterance["end"])
        words = _timed_records(utterance.get("words"))
        if not words and str(utterance.get("transcript", "")).strip():
            raise TranscriptTimingError(
                "A nonblank utterance requires source-timed words."
            )
        _validate_ordered_records(
            words,
            duration_seconds=duration,
            tolerance_seconds=tolerance_seconds,
        )
        if any(
            float(word["start"]) + tolerance_seconds < utterance_start
            or float(word["end"]) > utterance_end + tolerance_seconds
            for word in words
        ):
            raise TranscriptTimingError(
                "An utterance word falls outside its source-timed utterance."
            )


def _speaker_labels(record: Mapping[str, Any]) -> frozenset[str]:
    labels: set[str] = set()
    speaker = record.get("chunk_speaker")
    if isinstance(speaker, int) and not isinstance(speaker, bool) and speaker >= 0:
        labels.update(
            {
                str(speaker),
                f"chunk speaker {speaker}",
                f"chunk_speaker:{speaker}",
            }
        )
    cluster = record.get("local_voice_cluster_id")
    if isinstance(cluster, str) and cluster:
        labels.update({cluster, f"voice cluster {cluster}"})
    subject = record.get("reconciled_subject_id")
    if isinstance(subject, str) and subject:
        labels.update({subject, f"subject {subject}"})
    return frozenset(labels)


def _source_intervals(
    structured_source: Mapping[str, Any],
) -> dict[int, list[tuple[float, float, frozenset[str]]]]:
    transcript = structured_source.get("transcript")
    streams = transcript.get("streams") if isinstance(transcript, Mapping) else None
    if not isinstance(streams, list) or not streams:
        raise TranscriptTimingError("The consented structured transcript is invalid.")
    result: dict[int, list[tuple[float, float, frozenset[str]]]] = {}
    for stream in streams:
        if not isinstance(stream, Mapping):
            raise TranscriptTimingError(
                "The consented structured transcript is invalid."
            )
        stream_index = stream.get("stream_index")
        if (
            not isinstance(stream_index, int)
            or isinstance(stream_index, bool)
            or stream_index < 0
            or stream_index in result
        ):
            raise TranscriptTimingError(
                "The consented structured transcript has invalid stream indexes."
            )
        records = _timed_records(stream.get("words")) + _timed_records(
            stream.get("utterances")
        )
        intervals: list[tuple[float, float, frozenset[str]]] = []
        for record in records:
            start = _number(record.get("start"))
            end = _number(record.get("end"))
            if start is None or end is None or start < 0 or end < start:
                raise TranscriptTimingError(
                    "The consented structured transcript has invalid source times."
                )
            intervals.append((start, end, _speaker_labels(record)))
        if not intervals:
            raise TranscriptTimingError(
                "The consented structured transcript has no timed source evidence."
            )
        result[stream_index] = intervals
    return result


def _range_is_covered(
    *,
    start: float,
    end: float,
    intervals: Iterable[tuple[float, float, frozenset[str]]],
    speaker_label: str | None,
    tolerance_seconds: float,
) -> bool:
    cursor = start
    for source_start, source_end, labels in sorted(
        intervals, key=lambda value: (value[0], value[1], sorted(value[2]))
    ):
        if speaker_label is not None and speaker_label not in labels:
            continue
        if source_end + tolerance_seconds < cursor:
            continue
        if source_start > cursor + tolerance_seconds:
            return False
        cursor = max(cursor, source_end)
        if cursor + tolerance_seconds >= end:
            return True
    return False


def validate_followup_transcript_references(
    analysis: Mapping[str, Any],
    *,
    structured_source: Mapping[str, Any],
    tolerance_seconds: float = TIMING_TOLERANCE_SECONDS,
) -> None:
    """Require every model time/speaker reference to exist in transferred JSON."""
    intervals_by_stream = _source_intervals(structured_source)
    for key, speaker_required in (
        ("speaker_turn_summaries", True),
        ("important_timestamps", False),
    ):
        records = analysis.get(key)
        if not isinstance(records, list) or any(
            not isinstance(record, Mapping) for record in records
        ):
            raise TranscriptTimingError("Transcript analysis timing is invalid.")
        for record in records:
            start = _number(record.get("start_seconds"))
            end = _number(record.get("end_seconds"))
            stream_index = record.get("stream_index")
            if start is None or end is None or start < 0 or end < start:
                raise TranscriptTimingError("Transcript analysis timing is invalid.")
            if stream_index is None:
                candidate_streams = tuple(intervals_by_stream)
            elif (
                isinstance(stream_index, int)
                and not isinstance(stream_index, bool)
                and stream_index in intervals_by_stream
            ):
                candidate_streams = (stream_index,)
            else:
                raise TranscriptTimingError(
                    "Transcript analysis cites an unknown source stream."
                )
            speaker_label = record.get("speaker_label") if speaker_required else None
            if speaker_required and (
                not isinstance(speaker_label, str) or not speaker_label
            ):
                raise TranscriptTimingError(
                    "A speaker turn must cite a source speaker label."
                )
            if not any(
                _range_is_covered(
                    start=start,
                    end=end,
                    intervals=intervals_by_stream[candidate],
                    speaker_label=speaker_label if speaker_required else None,
                    tolerance_seconds=tolerance_seconds,
                )
                for candidate in candidate_streams
            ):
                raise TranscriptTimingError(
                    "Transcript analysis cites timing or a speaker absent from the "
                    "consented structured transcript."
                )

"""Pure, deterministic planning for traceable audio chunks."""

import math
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

DEFAULT_MAX_CHUNK_SECONDS = 10 * 60.0
DEFAULT_SILENCE_WINDOW_SECONDS = 30.0
DEFAULT_HARD_CUT_OVERLAP_SECONDS = 1.0
DEFAULT_MIN_SILENCE_SECONDS = 1.0
MAX_PROVIDER_SPLIT_DEPTH = 8
MAX_PROVIDER_SPLIT_ENCODINGS = 64


@dataclass(frozen=True)
class TimeRange:
    start_seconds: float
    end_seconds: float
    label: str = ""

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class AudioChunk:
    index: int
    start_seconds: float
    end_seconds: float
    overlap_before_seconds: float
    end_reason: str

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def as_dict(self) -> dict[str, int | float | str]:
        return {
            "index": self.index,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "duration_seconds": self.duration_seconds,
            "overlap_before_seconds": self.overlap_before_seconds,
            "end_reason": self.end_reason,
        }


@dataclass(frozen=True)
class AudioSplitPlan:
    duration_seconds: float
    effective_max_chunk_seconds: float
    provider_cap_applied: bool
    chunks: tuple[AudioChunk, ...]
    settings: dict[str, float | int | None]

    def as_dict(self) -> dict[str, Any]:
        return deepcopy(
            {
                "duration_seconds": self.duration_seconds,
                "effective_max_chunk_seconds": self.effective_max_chunk_seconds,
                "provider_cap_applied": self.provider_cap_applied,
                "settings": self.settings,
                "chunks": [chunk.as_dict() for chunk in self.chunks],
            }
        )


def split_audio_chunk_for_payload_limit(
    chunk: AudioChunk,
    *,
    hard_cut_overlap_seconds: float = DEFAULT_HARD_CUT_OVERLAP_SECONDS,
) -> tuple[AudioChunk, AudioChunk]:
    """Bisect an encoded chunk that exceeded the adapter byte ceiling.

    This is intentionally a hard source-time cut: the byte size is known only
    after encoding, after chapter and silence boundaries have already been
    applied. The right child receives the fixed hard-cut overlap while the left
    child retains any overlap inherited from the parent.
    """
    overlap = _finite_nonnegative(
        hard_cut_overlap_seconds,
        "hard_cut_overlap_seconds",
    )
    start = _finite_nonnegative(chunk.start_seconds, "chunk start")
    end = _finite_nonnegative(chunk.end_seconds, "chunk end")
    if end <= start:
        raise ValueError("chunk offsets must be increasing")

    cut = round(start + ((end - start) / 2), 6)
    right_start = round(cut - overlap, 6)
    # Both children must make meaningful progress. With the required one-second
    # overlap, a segment of two seconds or less cannot safely be bisected again.
    if cut <= start or cut >= end or right_start <= start:
        raise ValueError("audio chunk is too short for another hard-cut split")

    return (
        AudioChunk(
            index=chunk.index,
            start_seconds=round(start, 6),
            end_seconds=cut,
            overlap_before_seconds=round(chunk.overlap_before_seconds, 6),
            end_reason="provider_payload_limit",
        ),
        AudioChunk(
            index=chunk.index,
            start_seconds=right_start,
            end_seconds=round(end, 6),
            overlap_before_seconds=round(overlap, 6),
            end_reason=chunk.end_reason,
        ),
    )


def _finite_nonnegative(value: float, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def _coerce_range(value: TimeRange | Mapping[str, Any]) -> TimeRange:
    if isinstance(value, TimeRange):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("time ranges must be TimeRange or mapping values")
    start = value.get("start_seconds", value.get("start_time_seconds"))
    end = value.get("end_seconds", value.get("end_time_seconds"))
    return TimeRange(float(start), float(end), str(value.get("label", "")))


def _normalized_ranges(
    values: Iterable[TimeRange | Mapping[str, Any]],
    *,
    duration_seconds: float,
) -> tuple[TimeRange, ...]:
    result = []
    for raw_value in values:
        value = _coerce_range(raw_value)
        start = _finite_nonnegative(value.start_seconds, "range start")
        end = _finite_nonnegative(value.end_seconds, "range end")
        start = min(start, duration_seconds)
        end = min(end, duration_seconds)
        if end <= start:
            continue
        result.append(TimeRange(start, end, value.label))
    return tuple(
        sorted(result, key=lambda value: (value.start_seconds, value.end_seconds))
    )


def _deduplicate_boundaries(values: Iterable[float]) -> tuple[float, ...]:
    result: list[float] = []
    for value in sorted(values):
        if not result or not math.isclose(value, result[-1], abs_tol=1e-6):
            result.append(value)
    return tuple(result)


def plan_audio_splits(
    duration_seconds: float,
    *,
    chapters: Iterable[TimeRange | Mapping[str, Any]] = (),
    silences: Iterable[TimeRange | Mapping[str, Any]] = (),
    max_chunk_seconds: float = DEFAULT_MAX_CHUNK_SECONDS,
    preferred_silence_window_seconds: float = DEFAULT_SILENCE_WINDOW_SECONDS,
    hard_cut_overlap_seconds: float = DEFAULT_HARD_CUT_OVERLAP_SECONDS,
    minimum_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS,
    provider_max_bytes: int | None = None,
    estimated_bytes_per_second: float | None = None,
    provider_safety_factor: float = 0.95,
) -> AudioSplitPlan:
    """Plan chapter-bounded chunks, preferring nearby sustained silence.

    Provider byte limits are converted to a conservative duration only when the
    caller supplies the measured/known encoded byte rate. Actual generated bytes
    must still be checked before remote transfer.
    """
    duration = _finite_nonnegative(duration_seconds, "duration_seconds")
    maximum = _finite_nonnegative(max_chunk_seconds, "max_chunk_seconds")
    window = _finite_nonnegative(
        preferred_silence_window_seconds,
        "preferred_silence_window_seconds",
    )
    overlap = _finite_nonnegative(
        hard_cut_overlap_seconds,
        "hard_cut_overlap_seconds",
    )
    minimum_silence = _finite_nonnegative(
        minimum_silence_seconds,
        "minimum_silence_seconds",
    )
    if maximum == 0:
        raise ValueError("max_chunk_seconds must be positive")
    if not 0 < provider_safety_factor <= 1:
        raise ValueError("provider_safety_factor must be greater than 0 and at most 1")

    provider_cap_applied = False
    effective_maximum = maximum
    if provider_max_bytes is not None:
        if provider_max_bytes <= 0:
            raise ValueError("provider_max_bytes must be positive")
        if estimated_bytes_per_second is None:
            raise ValueError(
                "estimated_bytes_per_second is required with provider_max_bytes"
            )
        byte_rate = _finite_nonnegative(
            estimated_bytes_per_second,
            "estimated_bytes_per_second",
        )
        if byte_rate == 0:
            raise ValueError("estimated_bytes_per_second must be positive")
        provider_duration = provider_max_bytes * provider_safety_factor / byte_rate
        if provider_duration < effective_maximum:
            effective_maximum = provider_duration
            provider_cap_applied = True
    if effective_maximum <= overlap:
        raise ValueError("effective chunk duration must exceed hard-cut overlap")

    normalized_chapters = _normalized_ranges(chapters, duration_seconds=duration)
    normalized_silences = _normalized_ranges(silences, duration_seconds=duration)
    chapter_boundaries = _deduplicate_boundaries(
        [0.0, duration]
        + [
            value
            for chapter in normalized_chapters
            for value in (chapter.start_seconds, chapter.end_seconds)
        ]
    )
    silence_points = _deduplicate_boundaries(
        (silence.start_seconds + silence.end_seconds) / 2
        for silence in normalized_silences
        if silence.duration_seconds >= minimum_silence
    )

    if duration == 0:
        return AudioSplitPlan(
            duration_seconds=0.0,
            effective_max_chunk_seconds=effective_maximum,
            provider_cap_applied=provider_cap_applied,
            chunks=(),
            settings={
                "max_chunk_seconds": maximum,
                "preferred_silence_window_seconds": window,
                "hard_cut_overlap_seconds": overlap,
                "minimum_silence_seconds": minimum_silence,
                "provider_max_bytes": provider_max_bytes,
                "estimated_bytes_per_second": estimated_bytes_per_second,
            },
        )

    chunks: list[AudioChunk] = []
    pending_overlap = 0.0
    for boundary_index, (segment_start, segment_end) in enumerate(
        pairwise(chapter_boundaries)
    ):
        cursor = segment_start
        while segment_end - cursor > effective_maximum + 1e-6:
            target = cursor + effective_maximum
            nearby_silences = [
                point
                for point in silence_points
                if max(cursor, target - window) < point <= target
            ]
            if nearby_silences:
                cut = max(nearby_silences)
                end_reason = "silence"
                next_cursor = cut
                next_overlap = 0.0
            else:
                cut = target
                end_reason = (
                    "provider_limit" if provider_cap_applied else "max_duration"
                )
                next_cursor = cut - overlap
                next_overlap = overlap
            chunks.append(
                AudioChunk(
                    index=len(chunks),
                    start_seconds=round(cursor, 6),
                    end_seconds=round(cut, 6),
                    overlap_before_seconds=round(pending_overlap, 6),
                    end_reason=end_reason,
                )
            )
            cursor = next_cursor
            pending_overlap = next_overlap

        is_last_boundary = boundary_index == len(chapter_boundaries) - 2
        chunks.append(
            AudioChunk(
                index=len(chunks),
                start_seconds=round(cursor, 6),
                end_seconds=round(segment_end, 6),
                overlap_before_seconds=round(pending_overlap, 6),
                end_reason="end" if is_last_boundary else "chapter",
            )
        )
        pending_overlap = 0.0

    return AudioSplitPlan(
        duration_seconds=duration,
        effective_max_chunk_seconds=effective_maximum,
        provider_cap_applied=provider_cap_applied,
        chunks=tuple(chunks),
        settings={
            "max_chunk_seconds": maximum,
            "preferred_silence_window_seconds": window,
            "hard_cut_overlap_seconds": overlap,
            "minimum_silence_seconds": minimum_silence,
            "provider_max_bytes": provider_max_bytes,
            "estimated_bytes_per_second": estimated_bytes_per_second,
            "provider_safety_factor": provider_safety_factor,
        },
    )

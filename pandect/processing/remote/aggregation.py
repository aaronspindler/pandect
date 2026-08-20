"""Deterministic local composition of consented remote media results.

Remote model responses remain immutable per-request evidence.  This module only
normalizes their source-relative times and composes a reproducible view from
those durable results and local derivatives.  It performs no network I/O.
"""

from __future__ import annotations

import html
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import PurePath
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.identities.models import (
    IdentityAssertion,
    IdentityCandidate,
    IdentityObservation,
)
from pandect.identities.services import (
    VIDEO_FACE_AGGREGATE_ROLE,
    VIDEO_FACE_FRAME_ROLE,
    cosine_similarity,
    current_identity_candidates,
    decode_embedding,
)
from pandect.processing.extractors.hashing import (
    FileFingerprint,
    fingerprint_stream,
    sha256_bytes,
)
from pandect.processing.models import (
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
)
from pandect.processing.services import (
    ResultSpec,
    SourceUnavailableError,
    StageOutcome,
    StageProcessingError,
    enqueue_stage,
    mark_run_superseded,
    source_fingerprint,
)

AGGREGATE_PROCESSOR_NAME = "pandect.remote.deterministic-aggregate"
AGGREGATE_PROCESSOR_VERSION = "2"
MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
MAX_SUBTITLE_CUES = 50_000
MAX_TIMELINE_ENTRIES = 50_000
MAX_TIMELINE_ENTRY_CHARS = 16_384
MAX_ACCOUNT_TEXT_CHARS = 2_000_000
MAX_LOCAL_TIMELINE_BINDINGS = 10_000

VOICE_RECONCILIATION_REVISION = "campp-timed-complete-link-v1"
VOICE_MINIMUM_QUALITY_SCORE = 0.65
VOICE_MINIMUM_SAMPLE_DURATION_MS = 1_000
VOICE_MINIMUM_OVERLAP_SECONDS = 0.25
VOICE_MINIMUM_OBSERVATION_OVERLAP = 0.50
VOICE_OVERLAP_ASSIGNMENT_MARGIN = 0.20
VOICE_COSINE_SIMILARITY_THRESHOLD = 0.80
VOICE_COSINE_AMBIGUITY_MARGIN = 0.05

_REMOTE_INPUT_KINDS = frozenset(
    {
        ProcessingStage.Kind.REMOTE_IMAGE_DESCRIPTION,
        ProcessingStage.Kind.REMOTE_TRANSCRIPTION,
    }
)
_TERMINAL_STAGE_STATUSES = frozenset(
    {
        ProcessingStage.Status.SUCCEEDED,
        ProcessingStage.Status.FAILED,
        ProcessingStage.Status.SKIPPED,
        ProcessingStage.Status.SUPERSEDED,
    }
)
_LOCAL_VIDEO_INPUT_KINDS = frozenset(
    {
        ProcessingStage.Kind.VIDEO_SUBTITLES,
        ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        ProcessingStage.Kind.FACE_IDENTITY,
        ProcessingStage.Kind.VOICE_ANALYSIS,
        ProcessingStage.Kind.VOICE_IDENTITY,
    }
)
_LOCAL_AUDIO_INPUT_KINDS = frozenset(
    {
        ProcessingStage.Kind.VOICE_ANALYSIS,
        ProcessingStage.Kind.VOICE_IDENTITY,
    }
)
_TIMESTAMP_LINE = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[,.]\d{3})"
)
_HTML_TAG = re.compile(r"<[^>]*>")
_ASS_OVERRIDE = re.compile(r"\{[^}]*\}")
_TOKEN_EDGE = re.compile(r"(^\W+|\W+$)", re.UNICODE)


@dataclass(frozen=True)
class SubtitleParse:
    cues: tuple[dict[str, Any], ...]
    format: str
    truncated: bool = False
    decoding_replaced: bool = False


@dataclass(frozen=True)
class _VoiceEvidence:
    observation_id: str
    stream_index: int
    start: float
    end: float
    source_sha256: str
    feature_signature: tuple[str, int, str, str, str, str]
    values: tuple[float, ...]
    quality_score: float
    sample_duration_ms: int
    processor_name: str
    processor_version: str


@dataclass
class _SpeakerGroup:
    stream_index: int
    chunk_id: str
    chunk_speaker: int
    artifact_sha256: str
    records: list[dict[str, Any]]
    intervals: list[tuple[float, float]]
    evidence: list[_VoiceEvidence]
    evidence_matches: dict[str, dict[str, float]]
    feature_signature: tuple[str, int, str, str, str, str] | None = None
    representative: tuple[float, ...] | None = None
    minimum_internal_similarity: float | None = None
    ambiguity: str = ""

    @property
    def key(self) -> tuple[int, str, int]:
        return (self.stream_index, self.chunk_id, self.chunk_speaker)

    @property
    def start(self) -> float:
        return min((value[0] for value in self.intervals), default=0.0)


@dataclass
class _TimelineBindings:
    """Hashes and identifiers for exact local snapshots compiled into a timeline."""

    results: dict[str, str]
    artifacts: dict[str, dict[str, Any]]
    identity_observations: dict[str, str]
    identity_candidates: dict[str, str]

    @classmethod
    def empty(cls) -> _TimelineBindings:
        return cls({}, {}, {}, {})

    def input_hashes(self) -> dict[str, dict[str, Any]]:
        return {
            "local_results": dict(sorted(self.results.items())),
            "local_artifacts": dict(sorted(self.artifacts.items())),
            "local_identity_observations": dict(
                sorted(self.identity_observations.items())
            ),
            "local_identity_candidates": dict(sorted(self.identity_candidates.items())),
        }

    def provenance(self) -> dict[str, list[str]]:
        return {
            "local_input_result_ids": sorted(self.results),
            "local_input_artifact_ids": sorted(self.artifacts),
            "local_identity_observation_ids": sorted(self.identity_observations),
            "local_identity_candidate_ids": sorted(self.identity_candidates),
        }


def _seconds(value: Any, *, default: Decimal = Decimal(0)) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    if not parsed.is_finite():
        return default
    return parsed


def _json_seconds(value: Decimal) -> float:
    return round(float(value), 6)


def _clean_cue_text(value: str) -> str:
    value = html.unescape(_HTML_TAG.sub("", value))
    return "\n".join(line.rstrip() for line in value.splitlines()).strip()


def _clock_seconds(value: str) -> Decimal:
    normalized = value.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:
        hours, minutes, seconds = parts
    return Decimal(hours) * 3600 + Decimal(minutes) * 60 + Decimal(seconds)


def _parse_srt_or_vtt(value: str, *, format_name: str) -> SubtitleParse:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[dict[str, Any]] = []
    index = 0
    truncated = False
    while index < len(lines):
        match = _TIMESTAMP_LINE.search(lines[index])
        if match is None:
            index += 1
            continue
        start = _clock_seconds(match.group("start"))
        end = _clock_seconds(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index])
            index += 1
        text = _clean_cue_text("\n".join(text_lines))
        if text and end >= start:
            cues.append(
                {
                    "start": _json_seconds(start),
                    "end": _json_seconds(end),
                    "text": text,
                }
            )
        if len(cues) >= MAX_SUBTITLE_CUES:
            truncated = index < len(lines)
            break
    return SubtitleParse(tuple(cues), format_name, truncated=truncated)


def _ass_clock_seconds(value: str) -> Decimal:
    hours, minutes, seconds = value.strip().split(":")
    return Decimal(hours) * 3600 + Decimal(minutes) * 60 + Decimal(seconds)


def _parse_ass(value: str) -> SubtitleParse:
    in_events = False
    fields: list[str] = []
    cues: list[dict[str, Any]] = []
    truncated = False
    for line_number, line in enumerate(value.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.casefold() == "[events]"
            continue
        if not in_events:
            continue
        if stripped.casefold().startswith("format:"):
            fields = [part.strip().casefold() for part in stripped[7:].split(",")]
            continue
        if not stripped.casefold().startswith("dialogue:") or not fields:
            continue
        parts = stripped[9:].split(",", max(0, len(fields) - 1))
        if len(parts) != len(fields):
            continue
        row = dict(zip(fields, parts, strict=True))
        try:
            start = _ass_clock_seconds(row["start"])
            end = _ass_clock_seconds(row["end"])
        except (KeyError, InvalidOperation, ValueError):
            continue
        text = _ASS_OVERRIDE.sub("", row.get("text", ""))
        text = _clean_cue_text(text.replace(r"\N", "\n").replace(r"\n", "\n"))
        if text and end >= start:
            cues.append(
                {
                    "start": _json_seconds(start),
                    "end": _json_seconds(end),
                    "text": text,
                    "source_line": line_number,
                }
            )
        if len(cues) >= MAX_SUBTITLE_CUES:
            truncated = True
            break
    return SubtitleParse(tuple(cues), "ass", truncated=truncated)


def parse_subtitle_text(value: str, *, filename: str = "") -> SubtitleParse:
    """Parse common text subtitle derivatives into timestamped cues."""
    suffix = PurePath(filename).suffix.casefold()
    if suffix in {".ass", ".ssa"} or "[events]" in value.casefold():
        return _parse_ass(value)
    format_name = (
        "webvtt" if suffix == ".vtt" or value.lstrip().startswith("WEBVTT") else "srt"
    )
    return _parse_srt_or_vtt(value, format_name=format_name)


def _primary_alternative(data: dict[str, Any]) -> dict[str, Any]:
    channels = data.get("channels")
    if not isinstance(channels, list) or not channels:
        return {}
    first_channel = channels[0]
    alternatives = (
        first_channel.get("alternatives") if isinstance(first_channel, dict) else None
    )
    if not isinstance(alternatives, list) or not alternatives:
        return {}
    return alternatives[0] if isinstance(alternatives[0], dict) else {}


def _token_key(value: dict[str, Any]) -> str:
    token = str(value.get("punctuated_word") or value.get("word") or "")
    return _TOKEN_EDGE.sub("", token.casefold())


def _shift_timed_record(
    value: dict[str, Any],
    *,
    offset: Decimal,
    chunk_id: str,
) -> dict[str, Any] | None:
    if "start" not in value or "end" not in value:
        return None
    start = _seconds(value.get("start"))
    end = _seconds(value.get("end"))
    if end < start:
        return None
    shifted = dict(value)
    shifted["chunk_start"] = _json_seconds(start)
    shifted["chunk_end"] = _json_seconds(end)
    shifted["start"] = _json_seconds(offset + start)
    shifted["end"] = _json_seconds(offset + end)
    shifted["chunk_id"] = chunk_id
    if "speaker" in shifted:
        shifted["chunk_speaker"] = shifted.pop("speaker")
        shifted["speaker_scope"] = "chunk_local"
    return shifted


def _records_strongly_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start, left_end = float(left["start"]), float(left["end"])
    right_start, right_end = float(right["start"]), float(right["end"])
    overlap = min(left_end, right_end) - max(left_start, right_start)
    shortest = min(left_end - left_start, right_end - right_start)
    return overlap > 0 and (shortest <= 0 or overlap >= shortest * 0.5)


def _confidence(value: dict[str, Any]) -> float:
    confidence = value.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        return float(confidence)
    return -1.0


def _deduplicate_exact_prefix(
    accepted: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    overlap_start: float,
    overlap_end: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tail = [
        value
        for value in accepted
        if float(value["end"]) >= overlap_start - 0.25
        and float(value["start"]) <= overlap_end + 0.25
    ]
    prefix = [
        value for value in incoming if float(value["start"]) <= overlap_end + 0.25
    ]
    maximum = min(len(tail), len(prefix))
    match_count = 0
    for count in range(maximum, 0, -1):
        old = tail[-count:]
        new = prefix[:count]
        if all(
            _token_key(left)
            and _token_key(left) == _token_key(right)
            and abs(float(left["start"]) - float(right["start"])) <= 1.25
            for left, right in zip(old, new, strict=True)
        ):
            match_count = count
            break
    if not match_count:
        return incoming, []
    duplicates = incoming[:match_count]
    return incoming[match_count:], duplicates


def _merge_stream_words(
    chunks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    deduplicated: list[dict[str, Any]] = []
    alternates: list[dict[str, Any]] = []
    previous_end: float | None = None
    for chunk in chunks:
        incoming = [dict(value) for value in chunk["words"]]
        chunk_start = float(chunk["source_start_seconds"])
        chunk_end = float(chunk["source_end_seconds"])
        if previous_end is not None and chunk_start < previous_end:
            overlap_end = min(previous_end, chunk_end)
            incoming, duplicates = _deduplicate_exact_prefix(
                accepted,
                incoming,
                overlap_start=chunk_start,
                overlap_end=overlap_end,
            )
            deduplicated.extend(
                {
                    "chunk_id": value["chunk_id"],
                    "word": value.get("punctuated_word") or value.get("word", ""),
                    "start": value["start"],
                    "end": value["end"],
                    "reason": "exact_token_overlap",
                }
                for value in duplicates
            )

        for word in incoming:
            conflicting = [
                old
                for old in accepted
                if _token_key(old) != _token_key(word)
                and _records_strongly_overlap(old, word)
            ]
            if not conflicting:
                accepted.append(word)
                continue
            best_old = max(
                conflicting,
                key=lambda value: (_confidence(value), -float(value["start"])),
            )
            selected = word if _confidence(word) > _confidence(best_old) else best_old
            alternate = best_old if selected is word else word
            alternates.append(
                {
                    "start": min(float(best_old["start"]), float(word["start"])),
                    "end": max(float(best_old["end"]), float(word["end"])),
                    "reason": "ambiguous_overlap",
                    "selected": dict(selected),
                    "alternate": dict(alternate),
                }
            )
            if selected is word:
                accepted.remove(best_old)
                accepted.append(word)
        accepted.sort(
            key=lambda value: (
                float(value["start"]),
                float(value["end"]),
                value["chunk_id"],
            )
        )
        previous_end = max(previous_end or chunk_end, chunk_end)
    return accepted, deduplicated, alternates


def _join_words(words: list[dict[str, Any]]) -> str:
    return " ".join(
        str(value.get("punctuated_word") or value.get("word") or "").strip()
        for value in words
        if str(value.get("punctuated_word") or value.get("word") or "").strip()
    )


def merge_transcript_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Shift chunk-local times and deterministically reconcile hard-cut overlap."""
    normalized_chunks: list[dict[str, Any]] = []
    for position, chunk in enumerate(chunks):
        offset = _seconds(chunk.get("source_start_seconds"))
        source_end = _seconds(chunk.get("source_end_seconds"), default=offset)
        chunk_id = str(chunk.get("chunk_id") or f"chunk:{position:03d}")
        data = chunk.get("data") if isinstance(chunk.get("data"), dict) else {}
        alternative = _primary_alternative(data)
        words = [
            shifted
            for word in alternative.get("words", [])
            if isinstance(word, dict)
            and (shifted := _shift_timed_record(word, offset=offset, chunk_id=chunk_id))
            is not None
        ]
        utterances = [
            shifted
            for utterance in data.get("utterances", [])
            if isinstance(utterance, dict)
            and (
                shifted := _shift_timed_record(
                    utterance,
                    offset=offset,
                    chunk_id=chunk_id,
                )
            )
            is not None
        ]
        alternatives = []
        channels = data.get("channels")
        if isinstance(channels, list) and channels and isinstance(channels[0], dict):
            channel_alternatives = channels[0].get("alternatives", [])
            if isinstance(channel_alternatives, list):
                alternatives = [
                    {
                        "transcript": value.get("transcript", ""),
                        "confidence": value.get("confidence"),
                    }
                    for value in channel_alternatives[1:]
                    if isinstance(value, dict)
                ]
        normalized_chunks.append(
            {
                "chunk_id": chunk_id,
                "result_id": str(chunk.get("result_id", "")),
                "stage_id": str(chunk.get("stage_id", "")),
                "artifact_id": str(chunk.get("artifact_id", "")),
                "artifact_sha256": str(chunk.get("artifact_sha256", "")),
                "stream_index": chunk.get("stream_index"),
                "source_start_seconds": _json_seconds(offset),
                "source_end_seconds": _json_seconds(max(offset, source_end)),
                "transcript": str(alternative.get("transcript", chunk.get("text", ""))),
                "confidence": alternative.get("confidence", chunk.get("confidence")),
                "words": words,
                "utterances": utterances,
                "alternate_transcripts": alternatives,
            }
        )

    normalized_chunks.sort(
        key=lambda value: (
            -1 if value["stream_index"] is None else int(value["stream_index"]),
            float(value["source_start_seconds"]),
            value["chunk_id"],
        )
    )
    grouped: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for chunk in normalized_chunks:
        grouped[chunk["stream_index"]].append(chunk)

    streams: list[dict[str, Any]] = []
    all_alternates: list[dict[str, Any]] = []
    for stream_index, stream_chunks in sorted(
        grouped.items(), key=lambda value: -1 if value[0] is None else int(value[0])
    ):
        words, deduplicated, alternates = _merge_stream_words(stream_chunks)
        utterances = sorted(
            [value for chunk in stream_chunks for value in chunk["utterances"]],
            key=lambda value: (
                float(value["start"]),
                float(value["end"]),
                value["chunk_id"],
            ),
        )
        text = _join_words(words)
        if not text:
            text = "\n".join(
                value["transcript"].strip()
                for value in stream_chunks
                if value["transcript"].strip()
            )
        for value in alternates:
            value["stream_index"] = stream_index
        all_alternates.extend(alternates)
        streams.append(
            {
                "stream_index": stream_index,
                "text": text,
                "words": words,
                "utterances": utterances,
                "deduplicated_overlap_words": deduplicated,
                "ambiguous_alternates": alternates,
                "chunk_ids": [value["chunk_id"] for value in stream_chunks],
            }
        )

    merged_text = "\n\n".join(stream["text"] for stream in streams if stream["text"])
    return {
        "schema_version": "1",
        "merge_revision": "source-offset-exact-overlap-v1",
        "text": merged_text,
        "chunks": normalized_chunks,
        "streams": streams,
        "ambiguous_alternates": all_alternates,
        "speaker_reconciliation": {
            "default_scope": "chunk_local",
            "method": "not_reconciled_without_confirmed_local_voice_evidence",
        },
    }


def _valid_stream_index(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _merge_intervals(values: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(values):
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(value[0], value[1]) for value in merged]


def _interval_overlap(
    start: float,
    end: float,
    intervals: list[tuple[float, float]],
) -> float:
    return sum(
        max(0.0, min(end, right) - max(start, left)) for left, right in intervals
    )


def _normalize_voice_vector(values: tuple[float, ...]) -> tuple[float, ...]:
    magnitude = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(magnitude) or magnitude == 0:
        raise ValueError("Voice embedding has no finite direction.")
    return tuple(value / magnitude for value in values)


def _speaker_groups(merged: dict[str, Any]) -> list[_SpeakerGroup]:
    chunks = merged.get("chunks", [])
    chunk_hashes = {
        str(value.get("chunk_id", "")): str(value.get("artifact_sha256", ""))
        for value in chunks
        if isinstance(value, dict)
    }
    groups: dict[tuple[int, str, int], _SpeakerGroup] = {}
    for stream in merged.get("streams", []):
        if not isinstance(stream, dict):
            continue
        stream_index = _valid_stream_index(stream.get("stream_index"))
        if stream_index is None:
            continue
        for record_kind in ("words", "utterances"):
            for record in stream.get(record_kind, []):
                if not isinstance(record, dict):
                    continue
                speaker = record.get("chunk_speaker")
                chunk_id = str(record.get("chunk_id", ""))
                if (
                    not isinstance(speaker, int)
                    or isinstance(speaker, bool)
                    or speaker < 0
                    or not chunk_id
                ):
                    continue
                try:
                    start = float(record["start"])
                    end = float(record["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not math.isfinite(start) or not math.isfinite(end) or end <= start:
                    continue
                key = (stream_index, chunk_id, speaker)
                group = groups.setdefault(
                    key,
                    _SpeakerGroup(
                        stream_index=stream_index,
                        chunk_id=chunk_id,
                        chunk_speaker=speaker,
                        artifact_sha256=chunk_hashes.get(chunk_id, ""),
                        records=[],
                        intervals=[],
                        evidence=[],
                        evidence_matches={},
                    ),
                )
                group.records.append(record)
                group.intervals.append((start, end))
    for group in groups.values():
        group.intervals = _merge_intervals(group.intervals)
    return sorted(
        groups.values(),
        key=lambda value: (
            value.stream_index,
            value.start,
            value.chunk_id,
            value.chunk_speaker,
        ),
    )


def _eligible_voice_evidence(
    run: ProcessingRun,
    local_run_ids: tuple[UUID, ...],
) -> tuple[list[_VoiceEvidence], dict[str, int]]:
    rejected: defaultdict[str, int] = defaultdict(int)
    if not local_run_ids:
        return [], {"no_referenced_local_run": 1}
    allowed_runs = {str(value) for value in local_run_ids}
    observations = IdentityObservation.objects.filter(
        catalog_item=run.catalog_item,
        modality=IdentityObservation.Modality.VOICE,
    ).order_by("created_at", "pk")
    evidence = []
    for observation in observations:
        provenance = (
            observation.provenance if isinstance(observation.provenance, dict) else {}
        )
        if str(provenance.get("processing_run_id", "")) not in allowed_runs:
            rejected["unreferenced_local_run"] += 1
            continue
        if observation.quality_status != IdentityObservation.QualityStatus.APPROVED:
            rejected["quality_not_approved"] += 1
            continue
        quality_score = observation.quality_score
        if (
            not isinstance(quality_score, (int, float))
            or isinstance(quality_score, bool)
            or not math.isfinite(float(quality_score))
            or float(quality_score) < VOICE_MINIMUM_QUALITY_SCORE
        ):
            rejected["quality_score_below_threshold"] += 1
            continue
        duration_ms = observation.sample_duration_ms
        if (
            not isinstance(duration_ms, int)
            or isinstance(duration_ms, bool)
            or duration_ms < VOICE_MINIMUM_SAMPLE_DURATION_MS
        ):
            rejected["sample_too_short"] += 1
            continue
        location = (
            observation.source_location
            if isinstance(observation.source_location, dict)
            else {}
        )
        stream_index = _confirmed_voice_stream_index(location, provenance)
        try:
            start_ms = location["start_ms"]
            end_ms = location["end_ms"]
            valid_interval = (
                isinstance(start_ms, int)
                and not isinstance(start_ms, bool)
                and isinstance(end_ms, int)
                and not isinstance(end_ms, bool)
                and 0 <= start_ms < end_ms
            )
        except (KeyError, TypeError):
            valid_interval = False
        if stream_index is None or not valid_interval:
            rejected["source_location_invalid"] += 1
            continue
        try:
            values = _normalize_voice_vector(
                decode_embedding(
                    observation.embedding,
                    observation.embedding_dimension,
                )
            )
        except ValueError:
            rejected["embedding_invalid"] += 1
            continue
        evidence.append(
            _VoiceEvidence(
                observation_id=str(observation.pk),
                stream_index=stream_index,
                start=start_ms / 1000,
                end=end_ms / 1000,
                source_sha256=observation.source_sha256,
                feature_signature=(
                    observation.feature_space,
                    observation.embedding_dimension,
                    observation.model_identifier,
                    observation.model_version,
                    observation.model_sha256,
                    observation.preprocessing_version,
                ),
                values=values,
                quality_score=float(quality_score),
                sample_duration_ms=duration_ms,
                processor_name=observation.processor_name,
                processor_version=observation.processor_version,
            )
        )
    return evidence, dict(sorted(rejected.items()))


def _group_identifier(group: _SpeakerGroup) -> dict[str, Any]:
    return {
        "stream_index": group.stream_index,
        "chunk_id": group.chunk_id,
        "chunk_speaker": group.chunk_speaker,
    }


def _assign_voice_evidence(
    groups: list[_SpeakerGroup],
    evidence: list[_VoiceEvidence],
) -> list[dict[str, Any]]:
    ambiguities = []
    for observation in evidence:
        duration = observation.end - observation.start
        candidates = []
        for group in groups:
            if (
                group.stream_index != observation.stream_index
                or not group.artifact_sha256
                or group.artifact_sha256 != observation.source_sha256
            ):
                continue
            overlap = _interval_overlap(
                observation.start,
                observation.end,
                group.intervals,
            )
            fraction = overlap / duration
            if (
                overlap >= VOICE_MINIMUM_OVERLAP_SECONDS
                and fraction >= VOICE_MINIMUM_OBSERVATION_OVERLAP
            ):
                candidates.append((fraction, overlap, group))
        candidates.sort(
            key=lambda value: (
                -value[0],
                -value[1],
                value[2].key,
            )
        )
        if not candidates:
            continue
        if (
            len(candidates) > 1
            and candidates[0][0] - candidates[1][0] < VOICE_OVERLAP_ASSIGNMENT_MARGIN
        ):
            ambiguities.append(
                {
                    "reason": "observation_overlaps_multiple_provider_speakers",
                    "observation_id": observation.observation_id,
                    "candidates": [
                        {
                            **_group_identifier(value[2]),
                            "observation_overlap_fraction": round(value[0], 6),
                            "overlap_seconds": round(value[1], 6),
                        }
                        for value in candidates
                    ],
                }
            )
            continue
        selected_fraction, selected_overlap, selected_group = candidates[0]
        selected_group.evidence.append(observation)
        selected_group.evidence_matches[observation.observation_id] = {
            "observation_overlap_fraction": round(selected_fraction, 6),
            "overlap_seconds": round(selected_overlap, 6),
        }
    return ambiguities


def _prepare_group_representatives(
    groups: list[_SpeakerGroup],
) -> list[dict[str, Any]]:
    ambiguities = []
    for group in groups:
        if not group.evidence:
            continue
        signatures = {value.feature_signature for value in group.evidence}
        if len(signatures) != 1:
            group.ambiguity = "incompatible_local_feature_versions"
        else:
            group.feature_signature = next(iter(signatures))
            pairwise = [
                cosine_similarity(left.values, right.values)
                for index, left in enumerate(group.evidence)
                for right in group.evidence[index + 1 :]
            ]
            group.minimum_internal_similarity = min(pairwise) if pairwise else None
            if pairwise and min(pairwise) < VOICE_COSINE_SIMILARITY_THRESHOLD:
                group.ambiguity = "inconsistent_local_voice_observations"
            else:
                mean = tuple(
                    sum(value.values[index] for value in group.evidence)
                    / len(group.evidence)
                    for index in range(len(group.evidence[0].values))
                )
                try:
                    group.representative = _normalize_voice_vector(mean)
                except ValueError:
                    group.ambiguity = "inconsistent_local_voice_observations"
        if group.ambiguity:
            ambiguities.append(
                {
                    "reason": group.ambiguity,
                    **_group_identifier(group),
                    "observation_ids": sorted(
                        value.observation_id for value in group.evidence
                    ),
                    "feature_provenance_options": [
                        _signature_provenance(
                            signature,
                            [
                                value
                                for value in group.evidence
                                if value.feature_signature == signature
                            ],
                        )
                        for signature in sorted(signatures)
                    ],
                }
            )
    return ambiguities


def _cluster_voice_groups(
    groups: list[_SpeakerGroup],
) -> tuple[list[list[_SpeakerGroup]], list[dict[str, Any]]]:
    clusters: list[list[_SpeakerGroup]] = []
    ambiguities = []
    represented = [value for value in groups if value.representative is not None]
    for group in represented:
        candidates = []
        for cluster in clusters:
            if (
                cluster[0].stream_index != group.stream_index
                or cluster[0].feature_signature != group.feature_signature
                or any(value.chunk_id == group.chunk_id for value in cluster)
            ):
                continue
            similarities = [
                cosine_similarity(group.representative, value.representative)
                for value in cluster
                if value.representative is not None
            ]
            if similarities and min(similarities) >= VOICE_COSINE_SIMILARITY_THRESHOLD:
                candidates.append((min(similarities), cluster))
        candidates.sort(
            key=lambda value: (
                -value[0],
                value[1][0].start,
                value[1][0].key,
            )
        )
        if (
            len(candidates) > 1
            and candidates[0][0] - candidates[1][0] < VOICE_COSINE_AMBIGUITY_MARGIN
        ):
            group.ambiguity = "multiple_similarity_clusters"
            ambiguities.append(
                {
                    "reason": group.ambiguity,
                    **_group_identifier(group),
                    "candidate_minimum_cosines": [
                        {
                            "representative_group": _group_identifier(value[1][0]),
                            "cosine_similarity": round(value[0], 6),
                        }
                        for value in candidates
                    ],
                    "scores_are_probabilities": False,
                }
            )
            continue
        if candidates:
            candidates[0][1].append(group)
        else:
            clusters.append([group])
    return clusters, ambiguities


def _signature_provenance(
    signature: tuple[str, int, str, str, str, str],
    evidence: list[_VoiceEvidence],
) -> dict[str, Any]:
    return {
        "feature_space": signature[0],
        "embedding_dimension": signature[1],
        "model_identifier": signature[2],
        "model_version": signature[3],
        "model_sha256": signature[4],
        "preprocessing_version": signature[5],
        "processor_revisions": [
            {"name": name, "version": version}
            for name, version in sorted(
                {(value.processor_name, value.processor_version) for value in evidence}
            )
        ],
    }


def _feature_provenance(groups: list[_SpeakerGroup]) -> dict[str, Any]:
    signature = groups[0].feature_signature
    if signature is None:
        return {}
    return _signature_provenance(
        signature,
        [value for group in groups for value in group.evidence],
    )


def _unmatched_voice_groups(
    groups: list[_SpeakerGroup],
    reconciled_keys: set[tuple[int, str, int]],
) -> list[dict[str, Any]]:
    represented = [value for value in groups if value.representative is not None]
    unmatched = []
    for group in groups:
        if group.key in reconciled_keys:
            continue
        reason = group.ambiguity
        best_similarity = None
        if not reason and not group.evidence:
            reason = "no_eligible_timed_voice_observation"
        elif not reason and group.representative is None:
            reason = "no_compatible_group_representative"
        elif not reason:
            compatible = [
                value
                for value in represented
                if value is not group
                and value.stream_index == group.stream_index
                and value.chunk_id != group.chunk_id
                and value.feature_signature == group.feature_signature
            ]
            if compatible:
                best_similarity = max(
                    cosine_similarity(group.representative, value.representative)
                    for value in compatible
                    if group.representative is not None
                    and value.representative is not None
                )
                reason = "below_cosine_similarity_threshold"
            elif any(
                value is not group
                and value.stream_index == group.stream_index
                and value.chunk_id != group.chunk_id
                for value in represented
            ):
                reason = "incompatible_feature_version"
            else:
                reason = "no_other_compatible_chunk_speaker"
        value = {"reason": reason, **_group_identifier(group)}
        if group.evidence:
            value["observation_matches"] = [
                {
                    "observation_id": observation.observation_id,
                    "quality_score": observation.quality_score,
                    "sample_duration_ms": observation.sample_duration_ms,
                    **group.evidence_matches[observation.observation_id],
                }
                for observation in group.evidence
            ]
        if group.feature_signature is not None:
            value["feature_provenance"] = _feature_provenance([group])
        if best_similarity is not None:
            value["best_cosine_similarity"] = round(best_similarity, 6)
            value["scores_are_probabilities"] = False
        unmatched.append(value)
    return unmatched


def _reconcile_local_voice_speakers(
    merged: dict[str, Any],
    run: ProcessingRun,
    local_run_ids: tuple[UUID, ...],
) -> None:
    groups = _speaker_groups(merged)
    evidence, rejected = _eligible_voice_evidence(run, local_run_ids)
    ambiguities = _assign_voice_evidence(groups, evidence)
    ambiguities.extend(_prepare_group_representatives(groups))
    clusters, cluster_ambiguities = _cluster_voice_groups(groups)
    ambiguities.extend(cluster_ambiguities)

    eligible_clusters = [
        values for values in clusters if len({value.chunk_id for value in values}) >= 2
    ]
    eligible_clusters.sort(
        key=lambda values: (
            values[0].stream_index,
            min(value.start for value in values),
            values[0].key,
        )
    )
    cluster_values = []
    reconciled_keys: set[tuple[int, str, int]] = set()
    stream_ordinals: defaultdict[int, int] = defaultdict(int)
    for cluster in eligible_clusters:
        stream_index = cluster[0].stream_index
        stream_ordinals[stream_index] += 1
        cluster_id = (
            f"stream:{stream_index}/voice-cluster:{stream_ordinals[stream_index]:03d}"
        )
        similarities = [
            cosine_similarity(left.representative, right.representative)
            for index, left in enumerate(cluster)
            for right in cluster[index + 1 :]
            if left.representative is not None and right.representative is not None
        ]
        minimum_similarity = min(similarities)
        for group in cluster:
            reconciled_keys.add(group.key)
            observation_ids = sorted(value.observation_id for value in group.evidence)
            for record in group.records:
                record["local_voice_cluster_id"] = cluster_id
                record["speaker_scope"] = "stream_local_voice_cluster"
                record["speaker_reconciliation"] = (
                    "automatic_local_voice_embedding_similarity"
                )
                record["local_voice_observation_ids"] = observation_ids
        cluster_values.append(
            {
                "cluster_id": cluster_id,
                "stream_index": stream_index,
                "minimum_pairwise_cosine_similarity": round(
                    minimum_similarity,
                    6,
                ),
                "scores_are_probabilities": False,
                "groups": [
                    {
                        **_group_identifier(group),
                        "observation_matches": [
                            {
                                "observation_id": observation.observation_id,
                                "quality_score": observation.quality_score,
                                "sample_duration_ms": observation.sample_duration_ms,
                                **group.evidence_matches[observation.observation_id],
                            }
                            for observation in group.evidence
                        ],
                        "minimum_internal_cosine_similarity": (
                            round(group.minimum_internal_similarity, 6)
                            if group.minimum_internal_similarity is not None
                            else None
                        ),
                    }
                    for group in cluster
                ],
                "feature_provenance": _feature_provenance(cluster),
            }
        )

    merged["speaker_reconciliation"] = {
        "default_scope": "chunk_local",
        "method": VOICE_RECONCILIATION_REVISION,
        "stream_scoped": True,
        "unmatched_labels_remain_chunk_local": True,
        "operator_confirmation_has_higher_precedence": True,
        "scores_are_probabilities": False,
        "calibration_revision": "uncalibrated-cosine-heuristic-v1",
        "thresholds": {
            "required_quality_status": IdentityObservation.QualityStatus.APPROVED,
            "minimum_quality_score": VOICE_MINIMUM_QUALITY_SCORE,
            "minimum_sample_duration_ms": VOICE_MINIMUM_SAMPLE_DURATION_MS,
            "minimum_overlap_seconds": VOICE_MINIMUM_OVERLAP_SECONDS,
            "minimum_observation_overlap_fraction": (VOICE_MINIMUM_OBSERVATION_OVERLAP),
            "overlap_assignment_margin": VOICE_OVERLAP_ASSIGNMENT_MARGIN,
            "cosine_similarity_threshold": VOICE_COSINE_SIMILARITY_THRESHOLD,
            "cosine_ambiguity_margin": VOICE_COSINE_AMBIGUITY_MARGIN,
        },
        "feature_compatibility": (
            "Exact feature space, dimension, model identifier/version/digest, and "
            "preprocessing version are required."
        ),
        "local_run_ids": [str(value) for value in local_run_ids],
        "eligible_observation_count": len(evidence),
        "rejected_observation_counts": rejected,
        "provider_group_count": len(groups),
        "reconciled_group_count": len(reconciled_keys),
        "clusters": cluster_values,
        "ambiguities": ambiguities,
        "unmatched_groups": _unmatched_voice_groups(
            groups,
            reconciled_keys,
        ),
    }


def _referenced_local_run_ids(run: ProcessingRun) -> tuple[UUID, ...]:
    configuration = run.configuration if isinstance(run.configuration, dict) else {}
    artifact_ids = configuration.get("artifact_ids", [])
    ids = list(
        DerivedArtifact.objects.filter(
            catalog_item=run.catalog_item,
            pk__in=artifact_ids,
            stage__run__scope=ProcessingRun.Scope.LOCAL,
        )
        .values_list("stage__run_id", flat=True)
        .distinct()
    )
    if ids:
        return tuple(sorted(ids, key=str))
    newest = (
        ProcessingRun.objects.filter(
            catalog_item=run.catalog_item,
            scope=ProcessingRun.Scope.LOCAL,
            source_fingerprint=run.source_fingerprint,
            source_size=run.source_size,
        )
        .order_by("-created_at", "-pk")
        .values_list("pk", flat=True)
        .first()
    )
    return (newest,) if newest is not None else ()


def _local_inputs_are_terminal(
    run: ProcessingRun, local_run_ids: tuple[UUID, ...]
) -> bool:
    if not local_run_ids:
        return True
    kinds = (
        _LOCAL_VIDEO_INPUT_KINDS
        if run.catalog_item.item_type == CatalogItem.ItemType.VIDEO
        else _LOCAL_AUDIO_INPUT_KINDS
    )
    return (
        not ProcessingStage.objects.filter(
            run_id__in=local_run_ids,
            kind__in=kinds,
        )
        .exclude(status__in=_TERMINAL_STAGE_STATUSES)
        .exists()
    )


def _transcript_inputs(run: ProcessingRun) -> list[dict[str, Any]]:
    inputs = []
    results = (
        ProcessingResult.objects.filter(
            stage__run=run,
            stage__kind=ProcessingStage.Kind.REMOTE_TRANSCRIPTION,
            result_kind=ProcessingResult.Kind.TRANSCRIPT,
        )
        .select_related("stage")
        .order_by("stage__priority", "stage__unit_key", "created_at", "pk")
    )
    for result in results:
        provenance = result.provenance if isinstance(result.provenance, dict) else {}
        inputs.append(
            {
                "chunk_id": result.stage.unit_key,
                "result_id": str(result.pk),
                "stage_id": str(result.stage_id),
                "artifact_id": provenance.get("artifact_id", ""),
                "artifact_sha256": result.input_hashes.get("payload", ""),
                "stream_index": provenance.get("stream_index"),
                "source_start_seconds": provenance.get("source_start_seconds", 0),
                "source_end_seconds": provenance.get("source_end_seconds", 0),
                "data": result.data,
                "text": result.text,
                "confidence": result.confidence,
            }
        )
    return inputs


def _confirmed_voice_intervals(
    run: ProcessingRun,
    local_run_ids: tuple[UUID, ...],
) -> list[dict[str, Any]]:
    assertions = IdentityAssertion.objects.filter(
        status=IdentityAssertion.Status.ACTIVE,
        observation__catalog_item=run.catalog_item,
        observation__modality=IdentityObservation.Modality.VOICE,
    ).select_related("observation", "subject")
    intervals = []
    allowed_runs = {str(value) for value in local_run_ids}
    for assertion in assertions:
        observation = assertion.observation
        provenance = (
            observation.provenance if isinstance(observation.provenance, dict) else {}
        )
        if (
            allowed_runs
            and str(provenance.get("processing_run_id", "")) not in allowed_runs
        ):
            continue
        location = observation.source_location
        try:
            start = int(location["start_ms"]) / 1000
            end = int(location["end_ms"]) / 1000
        except (KeyError, TypeError, ValueError):
            continue
        stream_index = _confirmed_voice_stream_index(location, provenance)
        if stream_index is None:
            continue
        intervals.append(
            {
                "start": start,
                "end": end,
                "stream_index": stream_index,
                "subject_id": str(assertion.subject_id),
                "assertion_id": str(assertion.pk),
                "observation_id": str(observation.pk),
            }
        )
    return intervals


def _confirmed_voice_stream_index(
    location: dict[str, Any],
    provenance: dict[str, Any],
) -> int | None:
    """Return one unambiguous non-negative stream index for a voice interval."""
    raw_values = []
    if "stream_index" in location:
        raw_values.append(location["stream_index"])
    if "stream_index" in provenance:
        raw_values.append(provenance["stream_index"])
    audio_chunk = provenance.get("audio_chunk")
    if isinstance(audio_chunk, dict) and "stream_index" in audio_chunk:
        raw_values.append(audio_chunk["stream_index"])
    if not raw_values or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in raw_values
    ):
        return None
    values = set(raw_values)
    return values.pop() if len(values) == 1 else None


def _annotate_confirmed_speakers(
    merged: dict[str, Any],
    intervals: list[dict[str, Any]],
) -> None:
    reconciled = 0
    for stream in merged["streams"]:
        stream_index = stream.get("stream_index")
        if (
            not isinstance(stream_index, int)
            or isinstance(stream_index, bool)
            or stream_index < 0
        ):
            continue
        for key in ("words", "utterances"):
            for value in stream[key]:
                overlapping = [
                    interval
                    for interval in intervals
                    if interval["stream_index"] == stream_index
                    and min(float(value["end"]), interval["end"])
                    > max(float(value["start"]), interval["start"])
                ]
                subject_ids = {interval["subject_id"] for interval in overlapping}
                if len(subject_ids) != 1:
                    continue
                value["reconciled_subject_id"] = subject_ids.pop()
                value["speaker_reconciliation"] = "operator_confirmed_local_voice"
                value["speaker_scope"] = "operator_confirmed_subject"
                value["voice_assertion_ids"] = sorted(
                    {interval["assertion_id"] for interval in overlapping}
                )
                reconciled += 1
    if reconciled:
        reconciliation = merged.setdefault(
            "speaker_reconciliation",
            {
                "default_scope": "chunk_local",
                "method": "operator_confirmed_local_voice_intervals",
                "stream_scoped": True,
                "unmatched_labels_remain_chunk_local": True,
            },
        )
        reconciliation["operator_confirmed"] = {
            "method": "operator_confirmed_local_voice_intervals",
            "reconciled_record_count": reconciled,
            "higher_precedence_than_automatic_similarity": True,
        }


def _read_subtitle(artifact: DerivedArtifact) -> SubtitleParse:
    with artifact.file.open("rb") as stream:
        content = stream.read(MAX_SUBTITLE_BYTES + 1)
    byte_truncated = len(content) > MAX_SUBTITLE_BYTES
    content = content[:MAX_SUBTITLE_BYTES]
    try:
        text = content.decode("utf-8-sig")
        decoding_replaced = False
    except UnicodeDecodeError:
        text = content.decode("utf-8-sig", errors="replace")
        decoding_replaced = True
    parsed = parse_subtitle_text(text, filename=artifact.basename)
    return SubtitleParse(
        parsed.cues,
        parsed.format,
        truncated=parsed.truncated or byte_truncated,
        decoding_replaced=decoding_replaced,
    )


def _snapshot_sha256(value: Any) -> str:
    """Hash a JSON-safe evidence snapshot without exposing its content."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise StageProcessingError(
            "aggregate_local_snapshot_invalid",
            "A local evidence snapshot cannot be represented safely.",
        ) from error
    return sha256_bytes(encoded)


def _bind_result_snapshot(
    bindings: _TimelineBindings,
    result: ProcessingResult,
    _used_data: Any,
) -> None:
    snapshot = {
        "result_id": str(result.pk),
        "stage_id": str(result.stage_id),
        "run_id": str(result.stage.run_id),
        "stage_kind": result.stage.kind,
        "stage_unit_key": result.stage.unit_key,
        "stage_status": result.stage.status,
        "result_kind": result.result_kind,
        "evidence_tier": result.evidence_tier,
        "schema_version": result.schema_version,
        "text": result.text,
        "data": result.data,
        "confidence": result.confidence,
        "uncertainty": result.uncertainty,
        "processor_name": result.processor_name,
        "processor_version": result.processor_version,
        "prompt_template_version": result.prompt_template_version,
        "input_hashes": result.input_hashes,
        "provenance": result.provenance,
    }
    bindings.results[str(result.pk)] = _snapshot_sha256(snapshot)


def _bind_artifact_snapshot(
    bindings: _TimelineBindings,
    artifact: DerivedArtifact,
) -> None:
    artifact_id = str(artifact.pk)
    if artifact_id in bindings.artifacts:
        return
    try:
        with artifact.file.open("rb") as stream:
            fingerprint = fingerprint_stream(stream, preserve_position=False)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise StageProcessingError(
            "aggregate_local_artifact_unavailable",
            "A local derivative used by the aggregate is unavailable.",
        ) from error
    if (
        fingerprint.sha256 != artifact.sha256
        or fingerprint.size_bytes != artifact.byte_size
    ):
        raise StageProcessingError(
            "aggregate_local_artifact_integrity",
            "A local derivative used by the aggregate changed.",
        )
    snapshot = {
        "artifact_id": artifact_id,
        "stage_id": str(artifact.stage_id),
        "run_id": str(artifact.stage.run_id),
        "role": artifact.role,
        "mime_type": artifact.mime_type,
        "sha256": artifact.sha256,
        "byte_size": artifact.byte_size,
        "stream_index": artifact.stream_index,
        "frame_index": artifact.frame_index,
        "source_start_seconds": (
            str(artifact.source_start_seconds)
            if artifact.source_start_seconds is not None
            else None
        ),
        "source_end_seconds": (
            str(artifact.source_end_seconds)
            if artifact.source_end_seconds is not None
            else None
        ),
        "metadata": artifact.metadata,
    }
    bindings.artifacts[artifact_id] = {
        "content_sha256": fingerprint.sha256,
        "byte_size": fingerprint.size_bytes,
        "snapshot_sha256": _snapshot_sha256(snapshot),
    }


def _bind_face_observation_snapshot(
    bindings: _TimelineBindings,
    observation: IdentityObservation,
) -> None:
    tracking = observation.provenance.get("video_face_tracking", {})
    snapshot = {
        "observation_id": str(observation.pk),
        "unit_key": observation.unit_key,
        "source_location": observation.source_location,
        "source_sha256": observation.source_sha256,
        "feature_space": observation.feature_space,
        "processor_name": observation.processor_name,
        "processor_version": observation.processor_version,
        "model_identifier": observation.model_identifier,
        "model_version": observation.model_version,
        "model_sha256": observation.model_sha256,
        "preprocessing_version": observation.preprocessing_version,
        "quality_status": observation.quality_status,
        "quality_score": observation.quality_score,
        "quality_details": observation.quality_details,
        "processing_run_id": observation.provenance.get("processing_run_id"),
        "processing_stage_id": observation.provenance.get("processing_stage_id"),
        "identity_evidence_role": observation.provenance.get("identity_evidence_role"),
        "video_face_tracking": tracking,
    }
    # Do not include the opaque embedding or anything derived from its bytes.
    # The binding covers the observation's source, model, quality, role, and
    # immutable identifiers—the exact state used to compile the timeline.
    bindings.identity_observations[str(observation.pk)] = _snapshot_sha256(snapshot)


def _bind_identity_candidate_snapshot(
    bindings: _TimelineBindings,
    candidate: IdentityCandidate,
    *,
    assertion_id: str | None,
) -> None:
    snapshot = {
        "candidate_id": str(candidate.pk),
        "candidate_set_id": str(candidate.candidate_set_id),
        "producing_run_id": str(candidate.producing_run_id),
        "producing_stage_id": str(candidate.producing_stage_id),
        "observation_id": str(candidate.observation_id),
        "subject_id": str(candidate.subject_id),
        "subject_label": candidate.subject.label,
        "rank": candidate.rank,
        "similarity_score": candidate.similarity_score,
        "reference_count": candidate.reference_count,
        "calibration_revision": candidate.calibration_revision,
        "gallery_fingerprint": candidate.gallery_fingerprint,
        "decision": candidate.decision,
        "decided_at": (
            candidate.decided_at.isoformat() if candidate.decided_at else None
        ),
        "assertion_id": assertion_id,
    }
    bindings.identity_candidates[str(candidate.pk)] = _snapshot_sha256(snapshot)


def _safe_score_map(value: Any, *, labels: frozenset[str]) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    scores = {}
    for label in sorted(labels):
        score = value.get(label)
        if (
            isinstance(score, (int, float))
            and not isinstance(score, bool)
            and math.isfinite(float(score))
        ):
            scores[label] = round(float(score), 6)
    return scores


def _score_distribution(
    samples: list[dict[str, Any]],
    field: str,
) -> dict[str, dict[str, float | int]]:
    grouped: defaultdict[str, list[float]] = defaultdict(list)
    for sample in samples:
        value = sample.get(field)
        if not isinstance(value, dict):
            continue
        for label, score in value.items():
            grouped[label].append(float(score))
    return {
        label: {
            "sample_count": len(values),
            "minimum": round(min(values), 6),
            "maximum": round(max(values), 6),
            "mean": round(sum(values) / len(values), 6),
        }
        for label, values in sorted(grouped.items())
    }


_EXPRESSION_LABELS = frozenset(
    {"angry", "disgust", "fearful", "happy", "neutral", "sad", "surprised"}
)
_BINARY_VISUAL_LABELS = frozenset({"class_0", "class_1"})
_HEAD_POSE_LABELS = frozenset({"yaw", "pitch", "roll"})
_ADULT_AGE_BANDS = frozenset({"18-29", "30-44", "45-59", "60-75"})


def _safe_face_sample(
    result: ProcessingResult,
    frame: dict[str, Any],
    face: dict[str, Any],
) -> dict[str, Any] | None:
    timestamp = frame.get("timestamp_ms")
    frame_index = frame.get("frame_index")
    artifact_id = frame.get("artifact_id")
    if (
        not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not math.isfinite(float(timestamp))
        or float(timestamp) < 0
        or not isinstance(frame_index, int)
        or isinstance(frame_index, bool)
        or frame_index < 0
        or not artifact_id
    ):
        return None
    bbox = face.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        bbox = None
    elif not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        for value in bbox
    ):
        bbox = None
    age_band = face.get("apparent_age_band")
    return {
        "timestamp_ms": round(float(timestamp), 3),
        "frame_index": frame_index,
        "artifact_id": str(artifact_id),
        "result_id": str(result.pk),
        "bbox": [round(float(value), 6) for value in bbox] if bbox else None,
        "quality_score": face.get("quality_score"),
        "quality_assessment": face.get("quality_assessment"),
        "apparent_facial_expression_scores": _safe_score_map(
            face.get("apparent_facial_expression_scores"),
            labels=_EXPRESSION_LABELS,
        ),
        "apparent_age_band": age_band if age_band in _ADULT_AGE_BANDS else None,
        "binary_visual_class_scores": _safe_score_map(
            face.get("binary_visual_class_scores"),
            labels=_BINARY_VISUAL_LABELS,
        ),
        "head_pose_degrees": _safe_score_map(
            face.get("head_pose_degrees"),
            labels=_HEAD_POSE_LABELS,
        ),
    }


def _timeline_text(value: Any) -> tuple[str, bool]:
    text = str(value or "").strip()
    if len(text) <= MAX_TIMELINE_ENTRY_CHARS:
        return text, False
    return text[:MAX_TIMELINE_ENTRY_CHARS], True


def _keyframe_artifacts(
    run: ProcessingRun,
    local_run_ids: tuple[UUID, ...],
) -> dict[str, DerivedArtifact]:
    return {
        str(artifact.pk): artifact
        for artifact in DerivedArtifact.objects.filter(
            catalog_item=run.catalog_item,
            stage__run_id__in=local_run_ids,
            role=DerivedArtifact.Role.KEYFRAME,
        )
        .select_related("stage")
        .order_by("frame_index", "created_at", "pk")
    }


def _safe_polygon(value: Any) -> list[list[float]]:
    if not isinstance(value, list):
        return []
    polygon = []
    for point in value:
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(
                isinstance(coordinate, (int, float))
                and not isinstance(coordinate, bool)
                and math.isfinite(float(coordinate))
                for coordinate in point
            )
        ):
            return []
        polygon.append([round(float(coordinate), 6) for coordinate in point])
    return polygon


def _qr_timeline_entries(
    local_run_ids: tuple[UUID, ...],
    artifacts: dict[str, DerivedArtifact],
    bindings: _TimelineBindings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries = []
    warnings = []
    results = (
        ProcessingResult.objects.filter(
            stage__run_id__in=local_run_ids,
            stage__kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
            stage__status=ProcessingStage.Status.SUCCEEDED,
            result_kind=ProcessingResult.Kind.QR_CODES,
        )
        .select_related("stage")
        .order_by("stage__unit_key", "created_at", "pk")
    )
    for result in results:
        data = result.data if isinstance(result.data, dict) else {}
        used_frames = []
        for frame in data.get("frames", []):
            if not isinstance(frame, dict):
                continue
            timestamp = frame.get("timestamp_ms")
            frame_index = frame.get("frame_index")
            artifact_id = str(frame.get("artifact_id") or "")
            artifact = artifacts.get(artifact_id)
            codes = frame.get("codes")
            if (
                artifact is None
                or not isinstance(codes, list)
                or not isinstance(timestamp, (int, float))
                or isinstance(timestamp, bool)
                or not math.isfinite(float(timestamp))
                or float(timestamp) < 0
                or not isinstance(frame_index, int)
                or isinstance(frame_index, bool)
                or frame_index < 0
            ):
                if codes:
                    warnings.append(
                        {
                            "code": "qr_frame_reference_invalid",
                            "result_id": str(result.pk),
                        }
                    )
                continue
            used_codes = []
            for code_index, code in enumerate(codes):
                if not isinstance(code, dict):
                    continue
                text, truncated = _timeline_text(code.get("value"))
                polygon = _safe_polygon(code.get("polygon"))
                if not text and not polygon:
                    continue
                if not text:
                    text = "QR symbol detected; payload was not decoded."
                safe_code = {
                    "code_index": code_index,
                    "payload": text,
                    "payload_truncated": truncated,
                    "polygon": polygon,
                }
                used_codes.append(safe_code)
                entries.append(
                    {
                        "start": round(float(timestamp) / 1000, 6),
                        "end": round(float(timestamp) / 1000, 6),
                        "kind": "qr_observation",
                        "text": text,
                        "text_truncated": truncated,
                        "qr_payload": text,
                        "polygon": polygon,
                        "result_id": str(result.pk),
                        "artifact_id": artifact_id,
                        "artifact_sha256": artifact.sha256,
                        "frame_index": frame_index,
                        "evidence_tier": result.evidence_tier,
                        "uncertainty": (
                            "Local QR decoder observation. A decoded payload or "
                            "polygon can still be incomplete or a false positive."
                        ),
                    }
                )
            if used_codes:
                used_frames.append(
                    {
                        "timestamp_ms": round(float(timestamp), 3),
                        "frame_index": frame_index,
                        "artifact_id": artifact_id,
                        "codes": used_codes,
                    }
                )
                _bind_artifact_snapshot(bindings, artifact)
        if used_frames:
            _bind_result_snapshot(bindings, result, {"frames": used_frames})
    return entries, warnings


def _face_candidate_values(
    run: ProcessingRun,
    observation: IdentityObservation,
    bindings: _TimelineBindings,
) -> list[dict[str, Any]]:
    candidates = list(
        IdentityCandidate.objects.filter(observation=observation)
        .select_related("subject", "producing_run", "producing_stage")
        .order_by("rank", "pk")
    )
    current = current_identity_candidates(
        observation,
        candidates=candidates,
        canonical_source=FileFingerprint(
            sha256=run.source_fingerprint,
            size_bytes=run.source_size,
        ),
    )
    assertions = {
        value.candidate_id: value
        for value in IdentityAssertion.objects.filter(
            candidate_id__in=[value.pk for value in current],
            observation=observation,
            status=IdentityAssertion.Status.ACTIVE,
        )
    }
    values = []
    for candidate in current:
        assertion = assertions.get(candidate.pk)
        assertion_id = str(assertion.pk) if assertion else None
        _bind_identity_candidate_snapshot(
            bindings,
            candidate,
            assertion_id=assertion_id,
        )
        values.append(
            {
                "candidate_id": str(candidate.pk),
                "subject_id": str(candidate.subject_id),
                "subject_label": candidate.subject.label,
                "rank": candidate.rank,
                "raw_similarity": round(candidate.similarity_score, 6),
                "scores_are_probabilities": False,
                "reference_count": candidate.reference_count,
                "calibration_revision": candidate.calibration_revision,
                "decision": candidate.decision,
                "decided_at": (
                    candidate.decided_at.isoformat() if candidate.decided_at else None
                ),
                "assertion_id": assertion_id,
                "evidence_tier": (
                    ProcessingResult.EvidenceTier.OPERATOR_CONFIRMED
                    if assertion is not None
                    else ProcessingResult.EvidenceTier.MODEL_GENERATED
                ),
                "uncertainty": (
                    "Closed-gallery similarity candidate; the raw score is not a "
                    "probability or open-world identity conclusion."
                ),
            }
        )
    return values


def _face_track_timeline_entries(
    run: ProcessingRun,
    local_run_ids: tuple[UUID, ...],
    artifacts: dict[str, DerivedArtifact],
    bindings: _TimelineBindings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries = []
    warnings = []
    results = list(
        ProcessingResult.objects.filter(
            stage__run_id__in=local_run_ids,
            stage__kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
            stage__status=ProcessingStage.Status.SUCCEEDED,
            result_kind=ProcessingResult.Kind.FACE_OBSERVATIONS,
        )
        .select_related("stage")
        .order_by("stage__unit_key", "created_at", "pk")
    )
    result_by_id = {str(value.pk): value for value in results}
    samples_by_stage_track: defaultdict[tuple[str, str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for result in results:
        data = result.data if isinstance(result.data, dict) else {}
        for frame in data.get("frames", []):
            if not isinstance(frame, dict):
                continue
            for face in frame.get("faces", []):
                if not isinstance(face, dict):
                    continue
                track_id = face.get("track_id")
                if (
                    not isinstance(track_id, int)
                    or isinstance(track_id, bool)
                    or track_id < 1
                ):
                    continue
                sample = _safe_face_sample(result, frame, face)
                if sample is not None:
                    samples_by_stage_track[
                        (str(result.stage_id), f"track:{track_id:03d}")
                    ].append(sample)

    aggregate_results = [
        value
        for value in results
        if value.stage.unit_key in {"aggregate", "default"}
        and isinstance(value.data, dict)
        and isinstance(value.data.get("tracks"), list)
    ]
    observations = list(
        IdentityObservation.objects.filter(
            catalog_item=run.catalog_item,
            modality=IdentityObservation.Modality.FACE,
            provenance__processing_run_id__in=[str(value) for value in local_run_ids],
            provenance__identity_evidence_role=VIDEO_FACE_AGGREGATE_ROLE,
        ).order_by("created_at", "pk")
    )
    observations_by_stage_track = {
        (str(value.provenance.get("processing_stage_id", "")), value.unit_key): value
        for value in observations
    }
    member_ids = {
        str(member_id)
        for result in aggregate_results
        for track in result.data.get("tracks", [])
        if isinstance(track, dict)
        for member_id in track.get("member_observation_ids", [])
    }
    members = {
        str(value.pk): value
        for value in IdentityObservation.objects.filter(
            pk__in=member_ids,
            catalog_item=run.catalog_item,
            modality=IdentityObservation.Modality.FACE,
            provenance__processing_run_id__in=[str(value) for value in local_run_ids],
            provenance__identity_evidence_role=VIDEO_FACE_FRAME_ROLE,
        )
    }
    used_by_result: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for aggregate_result in aggregate_results:
        used_tracks = []
        for track in aggregate_result.data.get("tracks", []):
            if not isinstance(track, dict):
                continue
            track_id = track.get("track_id")
            if (
                not isinstance(track_id, int)
                or isinstance(track_id, bool)
                or track_id < 1
            ):
                continue
            unit_key = f"track:{track_id:03d}"
            aggregate_observation = observations_by_stage_track.get(
                (str(aggregate_result.stage_id), unit_key)
            )
            declared_member_ids = [
                str(value) for value in track.get("member_observation_ids", [])
            ]
            if aggregate_observation is not None:
                tracked_members = aggregate_observation.provenance.get(
                    "video_face_tracking", {}
                ).get("member_observation_ids", [])
                if [str(value) for value in tracked_members] != declared_member_ids:
                    warnings.append(
                        {
                            "code": "face_track_provenance_conflict",
                            "result_id": str(aggregate_result.pk),
                            "track_id": track_id,
                            "observation_id": str(aggregate_observation.pk),
                        }
                    )
                    aggregate_observation = None
            samples = []
            for member_id in declared_member_ids:
                member = members.get(member_id)
                if member is None:
                    warnings.append(
                        {
                            "code": "face_track_member_unavailable",
                            "result_id": str(aggregate_result.pk),
                            "track_id": track_id,
                            "observation_id": member_id,
                        }
                    )
                    continue
                stage_id = str(member.provenance.get("processing_stage_id", ""))
                matches = samples_by_stage_track.get((stage_id, member.unit_key), [])
                if len(matches) != 1:
                    warnings.append(
                        {
                            "code": "face_track_member_ambiguous",
                            "result_id": str(aggregate_result.pk),
                            "track_id": track_id,
                            "observation_id": member_id,
                        }
                    )
                    continue
                samples.append(dict(matches[0]))
            samples.sort(
                key=lambda value: (
                    float(value["timestamp_ms"]),
                    value["frame_index"],
                    value["result_id"],
                )
            )
            if not samples:
                continue
            for sample in samples:
                result = result_by_id[sample["result_id"]]
                used_by_result[str(result.pk)].append(sample)
                artifact = artifacts.get(sample["artifact_id"])
                if artifact is not None:
                    sample["artifact_sha256"] = artifact.sha256
                    _bind_artifact_snapshot(bindings, artifact)
            candidates = []
            observation_id = None
            if aggregate_observation is not None:
                observation_id = str(aggregate_observation.pk)
                _bind_face_observation_snapshot(bindings, aggregate_observation)
                candidates = _face_candidate_values(
                    run,
                    aggregate_observation,
                    bindings,
                )
            confirmed_labels = [
                value["subject_label"]
                for value in candidates
                if value["assertion_id"] is not None
            ]
            age_counts: defaultdict[str, int] = defaultdict(int)
            for sample in samples:
                age_band = sample.get("apparent_age_band")
                if age_band:
                    age_counts[age_band] += 1
            representative_timestamp = track.get("representative_timestamp_ms")
            representative = min(
                samples,
                key=lambda value: (
                    abs(
                        float(value["timestamp_ms"])
                        - float(representative_timestamp or 0)
                    ),
                    value["frame_index"],
                ),
            )
            text = f"Correlated face track across {len(samples)} keyframe(s)."
            if confirmed_labels:
                text += " Operator-confirmed local association: " + ", ".join(
                    confirmed_labels
                )
            result_ids = sorted(
                {str(aggregate_result.pk), *(value["result_id"] for value in samples)}
            )
            artifact_ids = sorted({value["artifact_id"] for value in samples})
            entry = {
                "start": round(float(samples[0]["timestamp_ms"]) / 1000, 6),
                "end": round(float(samples[-1]["timestamp_ms"]) / 1000, 6),
                "kind": "face_track_observation",
                "text": text,
                "text_truncated": False,
                "result_id": str(aggregate_result.pk),
                "result_ids": result_ids,
                "artifact_id": representative["artifact_id"],
                "artifact_ids": artifact_ids,
                "frame_index": representative["frame_index"],
                "face_track_observation_id": observation_id,
                "local_track_ordinal": track_id,
                "correlated_frame_count": len(samples),
                "quality_score": track.get("quality_score"),
                "quality_assessment": track.get("quality_assessment"),
                "samples": samples,
                "apparent_facial_expression_score_distribution": (
                    _score_distribution(
                        samples,
                        "apparent_facial_expression_scores",
                    )
                ),
                "apparent_adult_age_band_counts": dict(sorted(age_counts.items())),
                "anonymous_binary_visual_class_score_distribution": (
                    _score_distribution(samples, "binary_visual_class_scores")
                ),
                "head_pose_degree_distribution": _score_distribution(
                    samples,
                    "head_pose_degrees",
                ),
                "identity_candidates": candidates,
                "unknown_identity_remains_possible": True,
                "evidence_tier": ProcessingResult.EvidenceTier.MODEL_GENERATED,
                "uncertainty": (
                    "Frames are correlated by local SFace similarity. Apparent "
                    "expression, adult age band, anonymous binary visual classes, "
                    "head pose, and identity candidates are model hypotheses, not "
                    "emotion, intent, truth, sex, gender identity, or proof of "
                    "identity."
                ),
            }
            entries.append(entry)
            used_tracks.append(
                {
                    "track_id": track_id,
                    "member_observation_ids": declared_member_ids,
                    "aggregate_observation_id": observation_id,
                    "result_ids": result_ids,
                    "artifact_ids": artifact_ids,
                    "entry": entry,
                }
            )
        if used_tracks:
            _bind_result_snapshot(
                bindings,
                aggregate_result,
                {"tracks": used_tracks},
            )
    for result_id, used_samples in used_by_result.items():
        _bind_result_snapshot(
            bindings,
            result_by_id[result_id],
            {"face_samples": used_samples},
        )
    return entries, warnings


def _video_timeline(
    run: ProcessingRun,
    merged: dict[str, Any] | None,
    local_run_ids: tuple[UUID, ...],
) -> tuple[list[dict[str, Any]], dict[str, Any], _TimelineBindings]:
    entries: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    bindings = _TimelineBindings.empty()
    keyframes = _keyframe_artifacts(run, local_run_ids)

    descriptions = ProcessingResult.objects.filter(
        stage__run=run,
        stage__kind=ProcessingStage.Kind.REMOTE_IMAGE_DESCRIPTION,
        result_kind=ProcessingResult.Kind.IMAGE_DESCRIPTION,
    ).order_by("stage__priority", "created_at", "pk")
    for result in descriptions:
        provenance = result.provenance if isinstance(result.provenance, dict) else {}
        text, truncated = _timeline_text(result.text)
        artifact_id = str(provenance.get("artifact_id") or "")
        artifact = keyframes.get(artifact_id)
        if artifact is not None:
            _bind_artifact_snapshot(bindings, artifact)
        elif artifact_id:
            warnings.append(
                {
                    "code": "description_frame_reference_invalid",
                    "result_id": str(result.pk),
                    "artifact_id": artifact_id,
                }
            )
        entries.append(
            {
                "start": _json_seconds(
                    _seconds(provenance.get("source_start_seconds"))
                ),
                "end": _json_seconds(_seconds(provenance.get("source_end_seconds"))),
                "kind": "keyframe_description",
                "text": text,
                "text_truncated": truncated,
                "result_id": str(result.pk),
                "artifact_id": artifact_id or None,
                "artifact_sha256": artifact.sha256 if artifact is not None else None,
                "frame_index": provenance.get("frame_index"),
                "evidence_tier": result.evidence_tier,
                "model_observations": result.data,
            }
        )

    artifacts = (
        DerivedArtifact.objects.filter(
            catalog_item=run.catalog_item,
            stage__run_id__in=local_run_ids,
            role=DerivedArtifact.Role.SUBTITLE,
        )
        .select_related("stage")
        .order_by("stream_index", "created_at", "pk")
    )
    for artifact in artifacts:
        try:
            parsed = _read_subtitle(artifact)
        except (OSError, ValueError):
            warnings.append(
                {"code": "subtitle_unreadable", "artifact_id": str(artifact.pk)}
            )
            continue
        _bind_artifact_snapshot(bindings, artifact)
        for cue in parsed.cues:
            text, truncated = _timeline_text(cue["text"])
            entries.append(
                {
                    "start": cue["start"],
                    "end": cue["end"],
                    "kind": "subtitle",
                    "text": text,
                    "text_truncated": truncated,
                    "artifact_id": str(artifact.pk),
                    "stream_index": artifact.stream_index,
                    "format": parsed.format,
                    "evidence_tier": ProcessingResult.EvidenceTier.DETERMINISTIC,
                }
            )
        if parsed.truncated or parsed.decoding_replaced:
            warnings.append(
                {
                    "code": "subtitle_parse_limited",
                    "artifact_id": str(artifact.pk),
                    "truncated": parsed.truncated,
                    "decoding_replaced": parsed.decoding_replaced,
                }
            )

    ocr_results = (
        ProcessingResult.objects.filter(
            stage__run_id__in=local_run_ids,
            stage__kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
            stage__status=ProcessingStage.Status.SUCCEEDED,
            result_kind=ProcessingResult.Kind.OCR,
        )
        .select_related("stage")
        .order_by("stage__unit_key", "created_at", "pk")
    )
    for result in ocr_results:
        data = result.data if isinstance(result.data, dict) else {}
        used_frames = []
        for frame in data.get("frames", []):
            if not isinstance(frame, dict):
                continue
            ocr = frame.get("ocr") if isinstance(frame.get("ocr"), dict) else {}
            text, truncated = _timeline_text(ocr.get("text"))
            if not text:
                continue
            artifact_id = str(frame.get("artifact_id") or "")
            artifact = keyframes.get(artifact_id)
            if artifact is None:
                warnings.append(
                    {
                        "code": "ocr_frame_reference_invalid",
                        "result_id": str(result.pk),
                    }
                )
                continue
            try:
                timestamp_ms = float(frame.get("timestamp_ms", 0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(timestamp_ms) or timestamp_ms < 0:
                continue
            used_frame = {
                "timestamp_ms": round(timestamp_ms, 3),
                "frame_index": frame.get("frame_index"),
                "artifact_id": artifact_id,
                "text": text,
                "text_truncated": truncated,
            }
            used_frames.append(used_frame)
            _bind_artifact_snapshot(bindings, artifact)
            entries.append(
                {
                    "start": round(timestamp_ms / 1000, 6),
                    "end": round(timestamp_ms / 1000, 6),
                    "kind": "visible_text",
                    "text": text,
                    "text_truncated": truncated,
                    "result_id": str(result.pk),
                    "artifact_id": artifact_id,
                    "artifact_sha256": artifact.sha256,
                    "frame_index": frame.get("frame_index"),
                    "evidence_tier": result.evidence_tier,
                    "uncertainty": (
                        "Local OCR output; character recognition, reading order, "
                        "and source-region association can be incomplete."
                    ),
                }
            )
        if used_frames:
            _bind_result_snapshot(bindings, result, {"frames": used_frames})

    qr_entries, qr_warnings = _qr_timeline_entries(
        local_run_ids,
        keyframes,
        bindings,
    )
    entries.extend(qr_entries)
    warnings.extend(qr_warnings)

    face_entries, face_warnings = _face_track_timeline_entries(
        run,
        local_run_ids,
        keyframes,
        bindings,
    )
    entries.extend(face_entries)
    warnings.extend(face_warnings)

    if merged is not None:
        for stream in merged["streams"]:
            utterances = stream["utterances"]
            if utterances:
                transcript_entries = utterances
            else:
                transcript_entries = stream["words"]
            for value in transcript_entries:
                text, truncated = _timeline_text(
                    value.get("transcript")
                    or value.get("punctuated_word")
                    or value.get("word")
                )
                if not text:
                    continue
                entry = {
                    "start": value["start"],
                    "end": value["end"],
                    "kind": "transcript",
                    "text": text,
                    "text_truncated": truncated,
                    "chunk_id": value["chunk_id"],
                    "stream_index": stream["stream_index"],
                    "chunk_speaker": value.get("chunk_speaker"),
                    "speaker_scope": value.get("speaker_scope"),
                    "speaker_reconciliation": value.get("speaker_reconciliation"),
                    "local_voice_cluster_id": value.get("local_voice_cluster_id"),
                    "local_voice_observation_ids": value.get(
                        "local_voice_observation_ids"
                    ),
                    "reconciled_subject_id": value.get("reconciled_subject_id"),
                    "voice_assertion_ids": value.get("voice_assertion_ids"),
                    "evidence_tier": ProcessingResult.EvidenceTier.MODEL_GENERATED,
                }
                entries.append(entry)

    kind_order = {
        "subtitle": 0,
        "transcript": 1,
        "visible_text": 2,
        "qr_observation": 3,
        "face_track_observation": 4,
        "keyframe_description": 5,
    }
    entries.sort(
        key=lambda value: (
            float(value["start"]),
            kind_order.get(value["kind"], 99),
            str(value.get("result_id", "")),
            str(value.get("artifact_id", "")),
        )
    )
    if len(entries) > MAX_TIMELINE_ENTRIES:
        warnings.append(
            {
                "code": "timeline_entry_limit",
                "available": len(entries),
                "retained": MAX_TIMELINE_ENTRIES,
            }
        )
        entries = entries[:MAX_TIMELINE_ENTRIES]
    return entries, {"warnings": warnings}, bindings


def _format_clock(value: Any) -> str:
    total_ms = max(0, round(float(value) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _video_account_text(entries: list[dict[str, Any]]) -> tuple[str, bool]:
    labels = {
        "subtitle": "Subtitle",
        "transcript": "Transcript",
        "visible_text": "Visible text",
        "qr_observation": "QR observation",
        "face_track_observation": "Correlated face track",
        "keyframe_description": "Keyframe description",
    }
    lines = [
        f"[{_format_clock(value['start'])}] "
        f"{labels.get(value['kind'], value['kind'])}: "
        f"{value['text']}"
        for value in entries
    ]
    text = "\n".join(lines)
    if len(text) <= MAX_ACCOUNT_TEXT_CHARS:
        return text, False
    return text[:MAX_ACCOUNT_TEXT_CHARS], True


def _binding_ids(value: Any) -> tuple[UUID, ...] | None:
    if not isinstance(value, dict) or len(value) > MAX_LOCAL_TIMELINE_BINDINGS:
        return None
    try:
        return tuple(UUID(str(identifier)) for identifier in value)
    except (AttributeError, TypeError, ValueError):
        return None


def _binding_provenance_matches(
    provenance: dict[str, Any],
    bindings: _TimelineBindings,
) -> bool:
    expected = bindings.provenance()
    return all(provenance.get(key) == value for key, value in expected.items())


def verify_video_timeline_bindings(result: ProcessingResult) -> bool:
    """Revalidate every local snapshot bound to a durable video account.

    This is intentionally bounded and performs no remote I/O. It lets a later
    consent plan fail closed when a derivative file or mutable operator identity
    state changed after the deterministic aggregate was created.
    """
    if (
        result.result_kind != ProcessingResult.Kind.VIDEO_DESCRIPTION
        or result.processor_name != AGGREGATE_PROCESSOR_NAME
        or result.processor_version != AGGREGATE_PROCESSOR_VERSION
        or not isinstance(result.input_hashes, dict)
        or not isinstance(result.provenance, dict)
    ):
        return False
    input_hashes = result.input_hashes
    expected_results = input_hashes.get("local_results")
    expected_artifacts = input_hashes.get("local_artifacts")
    expected_observations = input_hashes.get("local_identity_observations")
    expected_candidates = input_hashes.get("local_identity_candidates")
    maps = (
        expected_results,
        expected_artifacts,
        expected_observations,
        expected_candidates,
    )
    identifiers = tuple(_binding_ids(value) for value in maps)
    if any(value is None for value in identifiers):
        return False
    if sum(len(value or ()) for value in identifiers) > MAX_LOCAL_TIMELINE_BINDINGS:
        return False
    try:
        local_run_ids = tuple(
            UUID(str(value)) for value in result.provenance.get("local_run_ids", [])
        )
    except (AttributeError, TypeError, ValueError):
        return False
    if not local_run_ids or len(local_run_ids) > 100:
        return False

    result_ids, artifact_ids, observation_ids, candidate_ids = identifiers
    current = _TimelineBindings.empty()
    local_results = list(
        ProcessingResult.objects.filter(
            pk__in=result_ids,
            stage__run_id__in=local_run_ids,
            stage__run__scope=ProcessingRun.Scope.LOCAL,
            stage__run__catalog_item_id=result.stage.run.catalog_item_id,
        ).select_related("stage__run")
    )
    if len(local_results) != len(result_ids):
        return False
    try:
        for local_result in local_results:
            _bind_result_snapshot(current, local_result, None)

        artifacts = list(
            DerivedArtifact.objects.filter(
                pk__in=artifact_ids,
                stage__run_id__in=local_run_ids,
                stage__run__scope=ProcessingRun.Scope.LOCAL,
                catalog_item_id=result.stage.run.catalog_item_id,
            ).select_related("stage__run")
        )
        if len(artifacts) != len(artifact_ids):
            return False
        for artifact in artifacts:
            _bind_artifact_snapshot(current, artifact)

        observations = list(
            IdentityObservation.objects.filter(
                pk__in=observation_ids,
                catalog_item_id=result.stage.run.catalog_item_id,
                modality=IdentityObservation.Modality.FACE,
                provenance__processing_run_id__in=[
                    str(value) for value in local_run_ids
                ],
                provenance__identity_evidence_role=VIDEO_FACE_AGGREGATE_ROLE,
            )
        )
        if len(observations) != len(observation_ids):
            return False
        for observation in observations:
            _bind_face_observation_snapshot(current, observation)

        candidates = list(
            IdentityCandidate.objects.filter(
                pk__in=candidate_ids,
                observation_id__in=observation_ids,
                observation__catalog_item_id=result.stage.run.catalog_item_id,
            ).select_related("subject", "producing_run", "producing_stage")
        )
        if len(candidates) != len(candidate_ids):
            return False
        assertions = {
            value.candidate_id: value
            for value in IdentityAssertion.objects.filter(
                candidate_id__in=candidate_ids,
                status=IdentityAssertion.Status.ACTIVE,
            )
        }
        for candidate in candidates:
            assertion = assertions.get(candidate.pk)
            _bind_identity_candidate_snapshot(
                current,
                candidate,
                assertion_id=str(assertion.pk) if assertion else None,
            )
    except StageProcessingError:
        return False

    current_source = FileFingerprint(
        sha256=result.stage.run.source_fingerprint,
        size_bytes=result.stage.run.source_size,
    )
    expected_candidate_ids = {str(value) for value in candidate_ids}
    for observation in observations:
        all_candidates = list(
            IdentityCandidate.objects.filter(observation=observation)
            .select_related("subject", "producing_run", "producing_stage")
            .order_by("rank", "pk")
        )
        try:
            current_ids = {
                str(value.pk)
                for value in current_identity_candidates(
                    observation,
                    candidates=all_candidates,
                    canonical_source=current_source,
                )
            }
        except (TypeError, ValueError):
            return False
        if current_ids != {
            str(value.pk)
            for value in candidates
            if value.observation_id == observation.pk
        }:
            return False
    if {str(value.pk) for value in candidates} != expected_candidate_ids:
        return False
    return current.input_hashes() == {
        "local_results": expected_results,
        "local_artifacts": expected_artifacts,
        "local_identity_observations": expected_observations,
        "local_identity_candidates": expected_candidates,
    } and _binding_provenance_matches(result.provenance, current)


def execute_remote_aggregate_stage(
    stage: ProcessingStage,
) -> StageOutcome:
    """Build one local-only aggregate outcome for an already claimed stage."""
    if stage.kind != ProcessingStage.Kind.REMOTE_AGGREGATE:
        raise StageProcessingError("aggregate_stage_invalid")
    run = stage.run
    try:
        current = source_fingerprint(run.catalog_item)
    except SourceUnavailableError:
        raise
    if (
        current.sha256 != run.source_fingerprint
        or current.size_bytes != run.source_size
    ):
        mark_run_superseded(run.pk)
        raise SourceUnavailableError("The canonical source changed.")
    local_run_ids = _referenced_local_run_ids(run)
    input_stages = list(run.stages.filter(kind__in=_REMOTE_INPUT_KINDS))
    if (
        not input_stages
        or any(value.status not in _TERMINAL_STAGE_STATUSES for value in input_stages)
        or not _local_inputs_are_terminal(run, local_run_ids)
    ):
        raise StageProcessingError(
            "aggregate_inputs_not_ready",
            "The durable aggregate inputs are not terminal.",
            retryable=True,
        )
    if not any(
        value.status == ProcessingStage.Status.SUCCEEDED for value in input_stages
    ):
        raise StageProcessingError(
            "aggregate_inputs_unavailable",
            "No consented request produced an aggregate input.",
        )
    transcript_inputs = _transcript_inputs(run)
    merged = merge_transcript_chunks(transcript_inputs) if transcript_inputs else None
    if merged is not None:
        _reconcile_local_voice_speakers(merged, run, local_run_ids)
        _annotate_confirmed_speakers(
            merged,
            _confirmed_voice_intervals(run, local_run_ids),
        )

    remote_results = list(
        ProcessingResult.objects.filter(
            stage__run=run,
            stage__kind__in=_REMOTE_INPUT_KINDS,
        ).order_by("created_at", "pk")
    )
    input_result_ids = [str(value.pk) for value in remote_results]
    input_hashes = {
        "source": run.source_fingerprint,
        "remote_results": {
            str(value.pk): value.input_hashes.get("payload", "")
            for value in remote_results
        },
    }
    common = {
        "evidence_tier": ProcessingResult.EvidenceTier.MODEL_GENERATED,
        "processor_name": AGGREGATE_PROCESSOR_NAME,
        "processor_version": AGGREGATE_PROCESSOR_VERSION,
        "input_hashes": input_hashes,
        "provenance": {
            "composition": "deterministic_local_postprocessing",
            "remote_run_id": str(run.pk),
            "input_result_ids": input_result_ids,
            "local_run_ids": [str(value) for value in local_run_ids],
            "additional_remote_requests": 0,
        },
    }
    results: list[ResultSpec] = []
    if merged is not None:
        failed_chunks = run.stages.filter(
            kind=ProcessingStage.Kind.REMOTE_TRANSCRIPTION,
            status=ProcessingStage.Status.FAILED,
        ).count()
        merged["failed_chunk_count"] = failed_chunks
        results.append(
            ResultSpec(
                result_kind=ProcessingResult.Kind.TRANSCRIPT,
                text=merged["text"],
                data=merged,
                uncertainty=(
                    "Deterministic merge of model-generated chunk transcripts. "
                    "Ambiguous overlap alternatives are retained; speaker labels "
                    "remain chunk-local unless conservative, compatible local "
                    "voice-embedding evidence reconciles them within one stream. "
                    "Cosine scores are similarity evidence, not probabilities; "
                    "operator-confirmed voice assertions take precedence."
                    + (
                        f" {failed_chunks} consented chunk(s) failed and are absent."
                        if failed_chunks
                        else ""
                    )
                ),
                **common,
            )
        )

    if run.catalog_item.item_type == CatalogItem.ItemType.VIDEO:
        entries, timeline_meta, timeline_bindings = _video_timeline(
            run,
            merged,
            local_run_ids,
        )
        account_text, account_truncated = _video_account_text(entries)
        data = {
            "schema_version": "2",
            "synthesis_method": "deterministic_timeline_compilation_v2",
            "additional_remote_requests": 0,
            "entries": entries,
            "entry_count": len(entries),
            "account_text_truncated": account_truncated,
            **timeline_meta,
        }
        results.append(
            ResultSpec(
                result_kind=ProcessingResult.Kind.VIDEO_DESCRIPTION,
                text=account_text,
                data=data,
                uncertainty=(
                    "Deterministic local timeline of model-generated descriptions and "
                    "transcripts plus local subtitles, OCR, QR observations, and "
                    "correlated face-track hypotheses. It does not infer continuity, "
                    "protected traits, truth, or open-world identity; resolve "
                    "contradictions; or make an additional remote request."
                ),
                **{
                    **common,
                    "input_hashes": {
                        **input_hashes,
                        **timeline_bindings.input_hashes(),
                    },
                    "provenance": {
                        **common["provenance"],
                        **timeline_bindings.provenance(),
                    },
                },
            )
        )
    return StageOutcome(results=tuple(results))


def finalize_remote_aggregate(run_id: UUID | str) -> bool:
    """Durably schedule one idempotent local aggregate for a remote media run."""
    with transaction.atomic():
        try:
            run = (
                ProcessingRun.objects.select_for_update()
                .select_related("catalog_item")
                .get(pk=run_id)
            )
        except ProcessingRun.DoesNotExist:
            return False
        if (
            run.scope != ProcessingRun.Scope.REMOTE
            or run.catalog_item.item_type
            not in {CatalogItem.ItemType.AUDIO, CatalogItem.ItemType.VIDEO}
            or run.status == ProcessingRun.Status.SUPERSEDED
        ):
            return False
        input_stages = list(run.stages.filter(kind__in=_REMOTE_INPUT_KINDS))
        if not input_stages or any(
            value.status not in _TERMINAL_STAGE_STATUSES for value in input_stages
        ):
            return False
        if not any(
            value.status == ProcessingStage.Status.SUCCEEDED for value in input_stages
        ):
            return False
        local_run_ids = _referenced_local_run_ids(run)
        if not _local_inputs_are_terminal(run, local_run_ids):
            return False

        now = timezone.now()
        stage, created = ProcessingStage.objects.get_or_create(
            run=run,
            kind=ProcessingStage.Kind.REMOTE_AGGREGATE,
            unit_key="default",
            defaults={
                "status": ProcessingStage.Status.PENDING,
                "max_attempts": 2,
                "priority": 32_000,
                "processor_name": AGGREGATE_PROCESSOR_NAME,
                "processor_version": AGGREGATE_PROCESSOR_VERSION,
            },
        )
        if not created:
            if stage.status in _TERMINAL_STAGE_STATUSES:
                return False
            if stage.status == ProcessingStage.Status.RUNNING:
                return False
            if stage.status == ProcessingStage.Status.QUEUED:
                return True
            if stage.status != ProcessingStage.Status.PENDING:
                return False
        ProcessingRun.objects.filter(pk=run.pk).update(
            status=ProcessingRun.Status.RUNNING,
            finished_at=None,
            updated_at=now,
        )
    return enqueue_stage(stage.pk)


def finalize_ready_remote_aggregates(*, limit: int = 100) -> int:
    """Bounded recovery for a crash between final remote commit and aggregation."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    run_ids = list(
        ProcessingRun.objects.filter(
            scope=ProcessingRun.Scope.REMOTE,
            catalog_item__item_type__in=(
                CatalogItem.ItemType.AUDIO,
                CatalogItem.ItemType.VIDEO,
            ),
            stages__kind__in=_REMOTE_INPUT_KINDS,
        )
        .exclude(status=ProcessingRun.Status.SUPERSEDED)
        .exclude(
            stages__kind=ProcessingStage.Kind.REMOTE_AGGREGATE,
            stages__status__in=_TERMINAL_STAGE_STATUSES,
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)
        .distinct()[:limit]
    )
    return sum(finalize_remote_aggregate(run_id) for run_id in run_ids)

"""FFprobe invocation and lossless JSON normalization."""

import json
import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from os import PathLike
from typing import Any

from pandect.processing.extractors.errors import InvalidExtractorOutputError
from pandect.processing.extractors.subprocesses import CommandResult, run_command

FFPROBE_TIMEOUT_SECONDS = 120


def _invalid_json_constant(_value: str) -> None:
    raise ValueError


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _rate(value: Any) -> float | None:
    if not isinstance(value, str):
        return _number(value)
    numerator, separator, denominator = value.partition("/")
    if not separator:
        return _number(value)
    top = _number(numerator)
    bottom = _number(denominator)
    if top is None or bottom in {None, 0.0}:
        return None
    return top / bottom


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class FFProbeMetadata:
    """Raw FFprobe output plus a stable, JSON-compatible technical summary."""

    raw: dict[str, Any]
    technical: dict[str, Any]

    @property
    def streams(self) -> list[dict[str, Any]]:
        return self.technical["streams"]

    @property
    def chapters(self) -> list[dict[str, Any]]:
        return self.technical["chapters"]

    def as_dict(self) -> dict[str, Any]:
        return {"raw": deepcopy(self.raw), "technical": deepcopy(self.technical)}


def _format_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "format_name": value.get("format_name"),
        "format_long_name": value.get("format_long_name"),
        "start_time_seconds": _number(value.get("start_time")),
        "duration_seconds": _number(value.get("duration")),
        "size_bytes": _integer(value.get("size")),
        "bit_rate": _integer(value.get("bit_rate")),
        "stream_count": _integer(value.get("nb_streams")),
        "program_count": _integer(value.get("nb_programs")),
        "probe_score": _integer(value.get("probe_score")),
        "tags": _mapping(value.get("tags")),
    }


def _stream_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    passthrough = (
        "codec_name",
        "codec_long_name",
        "profile",
        "codec_type",
        "codec_tag_string",
        "pix_fmt",
        "sample_aspect_ratio",
        "display_aspect_ratio",
        "field_order",
        "color_range",
        "color_space",
        "color_transfer",
        "color_primaries",
        "chroma_location",
        "sample_fmt",
        "channel_layout",
        "time_base",
        "start_pts",
        "duration_ts",
    )
    result = {name: value.get(name) for name in passthrough}
    result.update(
        {
            "index": _integer(value.get("index")),
            "width": _integer(value.get("width")),
            "height": _integer(value.get("height")),
            "coded_width": _integer(value.get("coded_width")),
            "coded_height": _integer(value.get("coded_height")),
            "bits_per_raw_sample": _integer(value.get("bits_per_raw_sample")),
            "sample_rate": _integer(value.get("sample_rate")),
            "channels": _integer(value.get("channels")),
            "start_time_seconds": _number(value.get("start_time")),
            "duration_seconds": _number(value.get("duration")),
            "bit_rate": _integer(value.get("bit_rate")),
            "frame_count": _integer(value.get("nb_frames")),
            "real_frame_rate": value.get("r_frame_rate"),
            "real_frame_rate_value": _rate(value.get("r_frame_rate")),
            "average_frame_rate": value.get("avg_frame_rate"),
            "average_frame_rate_value": _rate(value.get("avg_frame_rate")),
            "disposition": _mapping(value.get("disposition")),
            "tags": _mapping(value.get("tags")),
        }
    )
    return result


def _chapter_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _integer(value.get("id")),
        "time_base": value.get("time_base"),
        "start": value.get("start"),
        "start_time_seconds": _number(value.get("start_time")),
        "end": value.get("end"),
        "end_time_seconds": _number(value.get("end_time")),
        "tags": _mapping(value.get("tags")),
    }


def parse_ffprobe_json(value: str | bytes) -> FFProbeMetadata:
    """Validate FFprobe JSON while preserving unrecognized fields."""
    try:
        decoded = json.loads(value, parse_constant=_invalid_json_constant)
    except (UnicodeDecodeError, TypeError, ValueError) as error:
        raise InvalidExtractorOutputError from error
    if not isinstance(decoded, dict):
        raise InvalidExtractorOutputError

    raw = deepcopy(decoded)
    raw_format = raw.get("format")
    if raw_format is None:
        raw_format = {}
        raw["format"] = raw_format
    if not isinstance(raw_format, dict):
        raise InvalidExtractorOutputError
    # FFprobe echoes the machine-local input path. It is execution context, not
    # evidence metadata, and should not be persisted or rendered.
    source_path_redacted = "filename" in raw_format
    raw_format.pop("filename", None)

    raw_streams = raw.get("streams", [])
    raw_chapters = raw.get("chapters", [])
    if not isinstance(raw_streams, list) or not all(
        isinstance(item, dict) for item in raw_streams
    ):
        raise InvalidExtractorOutputError
    if not isinstance(raw_chapters, list) or not all(
        isinstance(item, dict) for item in raw_chapters
    ):
        raise InvalidExtractorOutputError

    technical = {
        "format": _format_summary(raw_format),
        "streams": [_stream_summary(stream) for stream in raw_streams],
        "chapters": [_chapter_summary(chapter) for chapter in raw_chapters],
        "source_path_redacted": source_path_redacted,
    }
    return FFProbeMetadata(raw=raw, technical=technical)


def probe_media(
    path: str | PathLike[str],
    *,
    timeout_seconds: float = FFPROBE_TIMEOUT_SECONDS,
    ffprobe_binary: str = "ffprobe",
    runner: Callable[..., CommandResult] = run_command,
) -> FFProbeMetadata:
    """Run FFprobe with no network access or input mutation."""
    result = runner(
        (
            ffprobe_binary,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-print_format",
            "json",
            "-i",
            path,
        ),
        timeout_seconds=timeout_seconds,
    )
    return parse_ffprobe_json(result.stdout)

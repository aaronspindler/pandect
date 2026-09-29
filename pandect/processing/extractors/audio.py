"""Deterministic FFmpeg helpers for acoustic facts and lossless audio chunks."""

import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from os import PathLike
from typing import Any

from pandect.media_runner.routing import isolated
from pandect.processing.extractors.audio_splits import TimeRange
from pandect.processing.extractors.errors import (
    InvalidExtractorOutputError,
    ToolFailedError,
)
from pandect.processing.extractors.hashing import FileFingerprint, fingerprint_file
from pandect.processing.extractors.subprocesses import CommandResult, run_command

MAX_FFMPEG_TIMEOUT_SECONDS = 2 * 60 * 60
_SILENCE_START_PATTERN = re.compile(
    r"silence_start:\s*(?P<seconds>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_SILENCE_END_PATTERN = re.compile(
    r"silence_end:\s*(?P<seconds>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_VOLUME_PATTERN = re.compile(
    r"(?P<name>mean_volume|max_volume):\s*"
    r"(?P<value>-?(?:\d+(?:\.\d+)?|inf))\s*dB",
    re.IGNORECASE,
)


def ffmpeg_timeout_seconds(duration_seconds: float | None) -> float:
    """Return the documented ``2 × duration + 300s`` timeout, capped at 2h."""
    if duration_seconds is None:
        return 300.0
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("duration_seconds must be finite and non-negative") from error
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration_seconds must be finite and non-negative")
    return min(2 * duration + 300, MAX_FFMPEG_TIMEOUT_SECONDS)


def parse_silencedetect(
    value: str | bytes,
    *,
    duration_seconds: float | None = None,
) -> tuple[TimeRange, ...]:
    """Parse ordered FFmpeg ``silencedetect`` intervals.

    An unterminated final silence is closed only when the media duration is
    known; otherwise it is omitted rather than assigned a fabricated endpoint.
    """
    try:
        text = (
            value.decode("utf-8", errors="strict")
            if isinstance(value, bytes)
            else value
        )
    except UnicodeDecodeError as error:
        raise InvalidExtractorOutputError from error
    if not isinstance(text, str):
        raise InvalidExtractorOutputError
    duration = None
    if duration_seconds is not None:
        duration = ffmpeg_timeout_seconds(duration_seconds)  # validate only
        duration = float(duration_seconds)

    ranges: list[TimeRange] = []
    pending_start: float | None = None
    for line in text.splitlines():
        start_match = _SILENCE_START_PATTERN.search(line)
        if start_match:
            start = max(0.0, float(start_match.group("seconds")))
            pending_start = min(start, duration) if duration is not None else start
        end_match = _SILENCE_END_PATTERN.search(line)
        if end_match and pending_start is not None:
            end = max(0.0, float(end_match.group("seconds")))
            if duration is not None:
                end = min(end, duration)
            if end > pending_start:
                ranges.append(TimeRange(pending_start, end, "silence"))
            pending_start = None
    if pending_start is not None and duration is not None and duration > pending_start:
        ranges.append(TimeRange(pending_start, duration, "silence"))
    return tuple(ranges)


@isolated("detect_silence", source="path")
def detect_silence(
    path: str | PathLike[str],
    *,
    duration_seconds: float,
    stream_index: int | None = None,
    noise_threshold_db: float = -35.0,
    minimum_duration_seconds: float = 1.0,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> tuple[TimeRange, ...]:
    """Run FFmpeg silence detection on one selected audio stream."""
    duration = float(duration_seconds)
    noise = float(noise_threshold_db)
    minimum = float(minimum_duration_seconds)
    if not all(math.isfinite(value) for value in (duration, noise, minimum)):
        raise ValueError("silence parameters must be finite")
    if duration < 0 or minimum <= 0:
        raise ValueError("duration must be non-negative and minimum silence positive")
    if stream_index is not None and stream_index < 0:
        raise ValueError("stream_index cannot be negative")

    argv: list[str | PathLike[str]] = [
        ffmpeg_binary,
        "-nostdin",
        "-hide_banner",
        "-v",
        "info",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-i",
        path,
    ]
    if stream_index is not None:
        argv.extend(("-map", f"0:{stream_index}"))
    argv.extend(
        (
            "-af",
            f"silencedetect=noise={noise:g}dB:d={minimum:g}",
            "-f",
            "null",
            "-",
        )
    )
    result = runner(
        tuple(argv),
        timeout_seconds=ffmpeg_timeout_seconds(duration),
    )
    return parse_silencedetect(result.stderr, duration_seconds=duration)


def _last_json_object(value: str | bytes) -> dict[str, Any]:
    try:
        text = (
            value.decode("utf-8", errors="strict")
            if isinstance(value, bytes)
            else value
        )
    except UnicodeDecodeError as error:
        raise InvalidExtractorOutputError from error
    if not isinstance(text, str):
        raise InvalidExtractorOutputError
    decoder = json.JSONDecoder(
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError())
    )
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            decoded, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(decoded, dict):
            candidates.append(decoded)
    if not candidates:
        raise InvalidExtractorOutputError
    return candidates[-1]


def _optional_decibel(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def parse_loudnorm(value: str | bytes) -> dict[str, float | None]:
    """Normalize the JSON summary emitted by FFmpeg's loudnorm filter."""
    raw = _last_json_object(value)
    aliases = {
        "integrated_loudness_lufs": "input_i",
        "true_peak_dbfs": "input_tp",
        "loudness_range_lu": "input_lra",
        "threshold_lufs": "input_thresh",
        "target_offset_lu": "target_offset",
    }
    result = {
        destination: _optional_decibel(raw.get(source))
        for destination, source in aliases.items()
    }
    if not any(measurement is not None for measurement in result.values()):
        raise InvalidExtractorOutputError
    return result


def parse_volumedetect(value: str | bytes) -> dict[str, float | bool | None]:
    """Parse mean and maximum sample volume from FFmpeg diagnostics."""
    try:
        text = (
            value.decode("utf-8", errors="strict")
            if isinstance(value, bytes)
            else value
        )
    except UnicodeDecodeError as error:
        raise InvalidExtractorOutputError from error
    if not isinstance(text, str):
        raise InvalidExtractorOutputError
    measurements: dict[str, float | None] = {}
    for match in _VOLUME_PATTERN.finditer(text):
        raw_value = match.group("value")
        measurements[match.group("name").casefold()] = _optional_decibel(raw_value)
    if not measurements:
        raise InvalidExtractorOutputError
    mean = measurements.get("mean_volume")
    peak = measurements.get("max_volume")
    return {
        "mean_volume_dbfs": mean,
        # FFmpeg volumedetect's mean_volume is the logarithmic full-scale RMS
        # measurement. Preserve its provider name and expose the forensic unit
        # explicitly so downstream consumers do not have to infer semantics.
        "rms_dbfs": mean,
        "rms_linear_full_scale": (
            round(10 ** (mean / 20), 12) if mean is not None else None
        ),
        "max_sample_peak_dbfs": peak,
        "possible_clipping": peak is not None and peak >= -0.1,
    }


@dataclass(frozen=True)
class AcousticMetadata:
    loudness: dict[str, float | None]
    volume: dict[str, float | bool | None]

    def as_dict(self) -> dict[str, Any]:
        return {"loudness": dict(self.loudness), "volume": dict(self.volume)}


@isolated("analyze_acoustics", source="path")
def analyze_acoustics(
    path: str | PathLike[str],
    *,
    duration_seconds: float,
    stream_index: int | None = None,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
    progress_callback: Callable[[], None] | None = None,
) -> AcousticMetadata:
    """Measure loudness range, true peak, mean volume, and clipping proxy."""
    if stream_index is not None and stream_index < 0:
        raise ValueError("stream_index cannot be negative")
    base: list[str | PathLike[str]] = [
        ffmpeg_binary,
        "-nostdin",
        "-hide_banner",
        "-v",
        "info",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-i",
        path,
    ]
    if stream_index is not None:
        base.extend(("-map", f"0:{stream_index}"))
    timeout = ffmpeg_timeout_seconds(duration_seconds)
    runner_options: dict[str, Any] = {"timeout_seconds": timeout}
    if progress_callback is not None:
        runner_options["progress_callback"] = progress_callback
    loudness_result = runner(
        tuple(
            [
                *base,
                "-af",
                "loudnorm=I=-23:LRA=7:TP=-2:print_format=json",
                "-f",
                "null",
                "-",
            ]
        ),
        **runner_options,
    )
    volume_result = runner(
        tuple([*base, "-af", "volumedetect", "-f", "null", "-"]),
        **runner_options,
    )
    return AcousticMetadata(
        loudness=parse_loudnorm(loudness_result.stderr),
        volume=parse_volumedetect(volume_result.stderr),
    )


@isolated("extract_audio_chunk", source="source_path", destination="destination_path")
def extract_audio_chunk(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
    *,
    start_seconds: float,
    end_seconds: float,
    stream_index: int | None = None,
    sample_rate: int = 16_000,
    channels: int = 1,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> FileFingerprint:
    """Materialize one exact-offset mono FLAC derivative without overwriting."""
    start = float(start_seconds)
    end = float(end_seconds)
    if (
        not all(math.isfinite(value) for value in (start, end))
        or start < 0
        or end <= start
    ):
        raise ValueError("audio chunk offsets must be finite and increasing")
    if stream_index is not None and stream_index < 0:
        raise ValueError("stream_index cannot be negative")
    if sample_rate <= 0 or channels <= 0:
        raise ValueError("sample_rate and channels must be positive")
    if os.path.abspath(source_path) == os.path.abspath(destination_path):
        raise ValueError("a derived chunk cannot overwrite its canonical source")
    if os.path.exists(destination_path):
        raise FileExistsError("the derived destination already exists")

    argv: list[str | PathLike[str]] = [
        ffmpeg_binary,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-i",
        source_path,
        "-ss",
        f"{start:.6f}",
        "-t",
        f"{end - start:.6f}",
    ]
    if stream_index is not None:
        argv.extend(("-map", f"0:{stream_index}"))
    argv.extend(
        (
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-c:a",
            "flac",
            "-f",
            "flac",
            "-threads",
            "1",
            "-n",
            destination_path,
        )
    )
    try:
        runner(
            tuple(argv),
            timeout_seconds=ffmpeg_timeout_seconds(end - start),
        )
    except Exception:
        try:
            os.unlink(destination_path)
        except OSError:
            pass
        raise
    if not os.path.isfile(destination_path):
        raise ToolFailedError
    return fingerprint_file(destination_path)

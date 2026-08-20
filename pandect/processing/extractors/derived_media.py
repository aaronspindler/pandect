"""Bounded, metadata-stripped derivatives for video processing stages."""

import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from os import PathLike
from typing import Any

from pandect.processing.extractors.audio import (
    extract_audio_chunk,
    ffmpeg_timeout_seconds,
)
from pandect.processing.extractors.errors import ToolFailedError
from pandect.processing.extractors.hashing import fingerprint_file
from pandect.processing.extractors.subprocesses import CommandResult, run_command

MAX_LOCAL_KEYFRAMES = 120
MAX_REMOTE_KEYFRAMES = 12
DEFAULT_SCENE_CHANGE_THRESHOLD = 0.3
DEFAULT_JPEG_QUALITY = 2
_SUBTITLE_CODECS = {
    "ass": ("ass", "text/x-ssa"),
    "ssa": ("ass", "text/x-ssa"),
    "subrip": ("srt", "application/x-subrip"),
    "srt": ("srt", "application/x-subrip"),
    "text": ("srt", "application/x-subrip"),
    "webvtt": ("vtt", "text/vtt"),
}
_SUBTITLE_ENCODERS = {
    "ass": "ass",
    "ssa": "ass",
    "subrip": "subrip",
    "srt": "subrip",
    "text": "subrip",
    "webvtt": "webvtt",
}
_ARTWORK_CODECS = {
    "bmp": ("bmp", "image/bmp"),
    "gif": ("gif", "image/gif"),
    "jpeg2000": ("jp2", "image/jp2"),
    "mjpeg": ("jpg", "image/jpeg"),
    "png": ("png", "image/png"),
    "tiff": ("tif", "image/tiff"),
    "webp": ("webp", "image/webp"),
}
_FAITHFUL_AUDIO_COPY_OUTPUTS = {
    "aac": ("aac", "audio/aac", "adts"),
    "ac3": ("ac3", "audio/ac3", "ac3"),
    "alac": ("m4a", "audio/mp4", "ipod"),
    "dts": ("dts", "audio/vnd.dts", "dts"),
    "eac3": ("eac3", "audio/eac3", "eac3"),
    "flac": ("flac", "audio/flac", "flac"),
    "mlp": ("mlp", "audio/vnd.dolby.mlp", "mlp"),
    "mp3": ("mp3", "audio/mpeg", "mp3"),
    "opus": ("ogg", "audio/ogg", "ogg"),
    "truehd": ("thd", "audio/vnd.dolby.mlp", "truehd"),
    "vorbis": ("ogg", "audio/ogg", "ogg"),
    "wavpack": ("wv", "audio/wavpack", "wv"),
}
FAITHFUL_AUDIO_STREAM_COPY_CODECS = tuple(sorted(_FAITHFUL_AUDIO_COPY_OUTPUTS))
_DECIMAL_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_SHOWINFO_TIME_PATTERN = re.compile(
    rf"\bpts_time:(?P<seconds>{_DECIMAL_PATTERN})\b",
    re.IGNORECASE,
)
_SCENE_SCORE_PATTERN = re.compile(
    rf"\blavfi\.scene_score=(?P<score>{_DECIMAL_PATTERN})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DerivedMediaFile:
    path: str
    mime_type: str
    sha256: str
    size_bytes: int
    stream_index: int | None = None
    frame_index: int | None = None
    source_start_seconds: float | None = None
    source_end_seconds: float | None = None
    metadata: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "stream_index": self.stream_index,
            "frame_index": self.frame_index,
            "source_start_seconds": self.source_start_seconds,
            "source_end_seconds": self.source_end_seconds,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class SceneChange:
    """One FFmpeg scene candidate with its source timestamp and score."""

    timestamp_seconds: float
    score: float

    def as_dict(self) -> dict[str, float]:
        return {
            "timestamp_seconds": self.timestamp_seconds,
            "score": self.score,
        }


@dataclass(frozen=True)
class RepresentativeFrame:
    """One chronologically ordered representative-frame selection."""

    timestamp_seconds: float
    reason: str
    scene_score: float | None = None

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "timestamp_seconds": self.timestamp_seconds,
            "reason": self.reason,
            "scene_score": self.scene_score,
        }


@dataclass(frozen=True)
class FaithfulAudioOutput:
    extension: str
    mime_type: str
    container: str
    output_codec: str
    stream_copy: bool


def faithful_audio_output_for_codec(codec_name: str | None) -> FaithfulAudioOutput:
    """Choose a packet-copy container or a lossless decoded-audio fallback."""
    normalized = codec_name.casefold().strip() if isinstance(codec_name, str) else ""
    if normalized.startswith("pcm_"):
        # NUT preserves FFmpeg's integer and floating-point PCM codec variants,
        # including 64-bit samples that WavPack would have to narrow.
        return FaithfulAudioOutput(
            extension="nut",
            mime_type="application/x-nut",
            container="nut",
            output_codec=normalized,
            stream_copy=True,
        )
    selected = _FAITHFUL_AUDIO_COPY_OUTPUTS.get(normalized)
    if selected is not None:
        extension, mime_type, container = selected
        return FaithfulAudioOutput(
            extension=extension,
            mime_type=mime_type,
            container=container,
            output_codec=normalized,
            stream_copy=True,
        )
    # WavPack supports integer and floating-point samples and preserves the
    # decoded source sample rate and channel count without lossy conversion.
    return FaithfulAudioOutput(
        extension="wv",
        mime_type="audio/wavpack",
        container="wv",
        output_codec="wavpack",
        stream_copy=False,
    )


def _validated_output(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
) -> tuple[str, str]:
    source = os.path.abspath(source_path)
    destination = os.path.abspath(destination_path)
    if source == destination:
        raise ValueError("a derivative cannot overwrite its canonical source")
    if os.path.lexists(destination):
        raise FileExistsError("the derived destination already exists")
    return source, destination


def _derived_result(
    path: str | PathLike[str],
    *,
    mime_type: str,
    stream_index: int | None = None,
    frame_index: int | None = None,
    source_start_seconds: float | None = None,
    source_end_seconds: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> DerivedMediaFile:
    if not os.path.isfile(path):
        raise ToolFailedError
    fingerprint = fingerprint_file(path)
    return DerivedMediaFile(
        path=os.fspath(path),
        mime_type=mime_type,
        sha256=fingerprint.sha256,
        size_bytes=fingerprint.size_bytes,
        stream_index=stream_index,
        frame_index=frame_index,
        source_start_seconds=source_start_seconds,
        source_end_seconds=source_end_seconds,
        metadata=metadata,
    )


def _run_to_new_file(
    runner: Callable[..., CommandResult],
    argv: tuple[str | PathLike[str], ...],
    *,
    destination: str,
    timeout_seconds: float,
) -> CommandResult:
    try:
        return runner(argv, timeout_seconds=timeout_seconds)
    except Exception:
        # The destination was verified absent and cannot be the canonical file,
        # so a partial derivative is safe to remove after tool failure.
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise


def representative_frame_selection(
    duration_seconds: float,
    *,
    maximum_frames: int,
    scene_changes: Sequence[SceneChange] = (),
    scene_change_times: Sequence[float] = (),
) -> tuple[RepresentativeFrame, ...]:
    """Choose endpoints, strongest scene changes, then even coverage.

    Scored candidates are ranked by descending score and then ascending timestamp.
    The deprecated unscored timestamps retain caller order for compatibility.
    Regardless of selection priority, the returned frames are chronological.
    """
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("duration_seconds must be finite and non-negative") from error
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration_seconds must be finite and non-negative")
    if not 1 <= maximum_frames <= MAX_LOCAL_KEYFRAMES:
        raise ValueError(f"maximum_frames must be between 1 and {MAX_LOCAL_KEYFRAMES}")
    if scene_changes and scene_change_times:
        raise ValueError("provide scored scene changes or unscored times, not both")

    scored_by_timestamp: dict[float, float] = {}
    for candidate in scene_changes:
        if not isinstance(candidate, SceneChange):
            raise ValueError("scene_changes must contain SceneChange values")
        try:
            timestamp = float(candidate.timestamp_seconds)
            score = float(candidate.score)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("scene changes must contain finite numbers") from error
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("scene scores must be between 0 and 1")
        if math.isfinite(timestamp) and 0 <= timestamp < duration:
            rounded_timestamp = round(timestamp, 6)
            scored_by_timestamp[rounded_timestamp] = max(
                score,
                scored_by_timestamp.get(rounded_timestamp, -1.0),
            )
    candidate_scenes: list[tuple[float, float | None]] = sorted(
        scored_by_timestamp.items(),
        key=lambda value: (-value[1], value[0]),
    )
    if not scene_changes:
        for value in scene_change_times:
            try:
                timestamp = float(value)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("scene change times must be finite numbers") from error
            if math.isfinite(timestamp) and 0 <= timestamp < duration:
                candidate_scenes.append((round(timestamp, 6), None))

    selected: list[RepresentativeFrame] = [RepresentativeFrame(0.0, "first_frame")]
    if duration > 0 and maximum_frames > 1:
        # A seek to the exact declared duration is past the final frame in many
        # containers. Sample just inside the interval instead.
        selected.append(
            RepresentativeFrame(
                max(0.0, duration - min(0.001, duration)),
                "last_frame",
            )
        )
    seen_scene_times: set[float] = set()
    for timestamp, score in candidate_scenes:
        if timestamp in seen_scene_times:
            continue
        seen_scene_times.add(timestamp)
        if len(selected) >= maximum_frames:
            break
        if not any(
            math.isclose(timestamp, current.timestamp_seconds, abs_tol=1e-3)
            for current in selected
        ):
            selected.append(RepresentativeFrame(timestamp, "scene_change", score))

    if len(selected) < maximum_frames and duration > 0:
        # Use max+1 intervals so endpoints plus interior samples cover the whole
        # clip even when scene changes consumed some slots.
        for step in range(1, maximum_frames):
            timestamp = duration * step / maximum_frames
            if not any(
                math.isclose(timestamp, current.timestamp_seconds, abs_tol=1e-3)
                for current in selected
            ):
                selected.append(RepresentativeFrame(timestamp, "even_coverage"))
            if len(selected) >= maximum_frames:
                break

    return tuple(
        RepresentativeFrame(
            round(value.timestamp_seconds, 6),
            value.reason,
            round(value.scene_score, 6) if value.scene_score is not None else None,
        )
        for value in sorted(
            selected[:maximum_frames],
            key=lambda candidate: candidate.timestamp_seconds,
        )
    )


def representative_frame_times(
    duration_seconds: float,
    *,
    maximum_frames: int,
    scene_changes: Sequence[SceneChange] = (),
    scene_change_times: Sequence[float] = (),
) -> tuple[float, ...]:
    """Return only timestamps for callers that do not need selection provenance."""
    return tuple(
        value.timestamp_seconds
        for value in representative_frame_selection(
            duration_seconds,
            maximum_frames=maximum_frames,
            scene_changes=scene_changes,
            scene_change_times=scene_change_times,
        )
    )


def parse_showinfo_timestamps(value: str | bytes) -> tuple[float, ...]:
    """Parse unique non-negative scene timestamps from FFmpeg showinfo output."""
    try:
        text = (
            value.decode("utf-8", errors="strict")
            if isinstance(value, bytes)
            else value
        )
    except UnicodeDecodeError as error:
        raise ValueError("showinfo output is not valid UTF-8") from error
    if not isinstance(text, str):
        raise ValueError("showinfo output must be text or bytes")
    values = {
        round(float(match.group("seconds")), 6)
        for match in _SHOWINFO_TIME_PATTERN.finditer(text)
        if float(match.group("seconds")) >= 0
    }
    return tuple(sorted(values))


def parse_scene_changes(value: str | bytes) -> tuple[SceneChange, ...]:
    """Parse FFmpeg metadata-print timestamps and ``lavfi.scene_score`` values."""
    try:
        text = (
            value.decode("utf-8", errors="strict")
            if isinstance(value, bytes)
            else value
        )
    except UnicodeDecodeError as error:
        raise ValueError("scene metadata output is not valid UTF-8") from error
    if not isinstance(text, str):
        raise ValueError("scene metadata output must be text or bytes")

    pending_timestamp: float | None = None
    scores_by_timestamp: dict[float, float] = {}
    for line in text.splitlines():
        timestamp_match = _SHOWINFO_TIME_PATTERN.search(line)
        if timestamp_match is not None:
            timestamp = float(timestamp_match.group("seconds"))
            pending_timestamp = timestamp if math.isfinite(timestamp) else None
            continue
        score_match = _SCENE_SCORE_PATTERN.search(line)
        if score_match is None or pending_timestamp is None:
            continue
        score = float(score_match.group("score"))
        if pending_timestamp >= 0 and math.isfinite(score) and 0 <= score <= 1:
            timestamp = round(pending_timestamp, 6)
            scores_by_timestamp[timestamp] = max(
                round(score, 6),
                scores_by_timestamp.get(timestamp, -1.0),
            )
        pending_timestamp = None

    return tuple(
        SceneChange(timestamp_seconds=timestamp, score=score)
        for timestamp, score in sorted(
            scores_by_timestamp.items(),
            key=lambda value: (-value[1], value[0]),
        )
    )


def detect_scene_changes(
    source_path: str | PathLike[str],
    *,
    duration_seconds: float,
    threshold: float = DEFAULT_SCENE_CHANGE_THRESHOLD,
    video_stream_index: int | None = None,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> tuple[SceneChange, ...]:
    """Detect bounded, score-ranked scene candidates without materializing frames."""
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration_seconds must be finite and non-negative")
    threshold_value = float(threshold)
    if not math.isfinite(threshold_value) or not 0 < threshold_value < 1:
        raise ValueError("threshold must be greater than 0 and less than 1")
    if video_stream_index is not None and video_stream_index < 0:
        raise ValueError("video_stream_index cannot be negative")

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
        source_path,
    ]
    if video_stream_index is not None:
        argv.extend(("-map", f"0:{video_stream_index}"))
    argv.extend(
        (
            "-vf",
            (
                f"select=gt(scene\\,{threshold_value:g}),"
                "metadata=mode=print:key=lavfi.scene_score"
            ),
            "-an",
            "-sn",
            "-dn",
            "-f",
            "null",
            "-",
        )
    )
    result = runner(
        tuple(argv),
        timeout_seconds=ffmpeg_timeout_seconds(duration),
    )
    return tuple(
        change
        for change in parse_scene_changes(result.stderr)
        if change.timestamp_seconds < duration
    )


def detect_scene_change_times(
    source_path: str | PathLike[str],
    *,
    duration_seconds: float,
    threshold: float = DEFAULT_SCENE_CHANGE_THRESHOLD,
    video_stream_index: int | None = None,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> tuple[float, ...]:
    """Compatibility wrapper returning scene timestamps in chronological order."""
    changes = detect_scene_changes(
        source_path,
        duration_seconds=duration_seconds,
        threshold=threshold,
        video_stream_index=video_stream_index,
        ffmpeg_binary=ffmpeg_binary,
        runner=runner,
    )
    return tuple(sorted(change.timestamp_seconds for change in changes))


def extract_video_frame(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
    *,
    timestamp_seconds: float,
    frame_index: int,
    duration_seconds: float,
    video_stream_index: int | None = None,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> DerivedMediaFile:
    """Extract one metadata-stripped JPEG frame at an exact source timestamp."""
    _, destination = _validated_output(source_path, destination_path)
    timestamp = float(timestamp_seconds)
    duration = float(duration_seconds)
    if (
        not math.isfinite(timestamp)
        or not math.isfinite(duration)
        or timestamp < 0
        or duration < 0
        or timestamp > duration
    ):
        raise ValueError("frame timestamp must be within the finite media duration")
    if frame_index < 0:
        raise ValueError("frame_index cannot be negative")
    if video_stream_index is not None and video_stream_index < 0:
        raise ValueError("video_stream_index cannot be negative")
    if not 1 <= jpeg_quality <= 31:
        raise ValueError("jpeg_quality must be between 1 and 31")

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
        "-ss",
        f"{timestamp:.6f}",
        "-i",
        source_path,
    ]
    if video_stream_index is not None:
        argv.extend(("-map", f"0:{video_stream_index}"))
    argv.extend(
        (
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-q:v",
            str(jpeg_quality),
            "-pix_fmt",
            "yuvj420p",
            "-f",
            "image2",
            "-threads",
            "1",
            "-n",
            destination,
        )
    )
    _run_to_new_file(
        runner,
        tuple(argv),
        destination=destination,
        timeout_seconds=ffmpeg_timeout_seconds(duration),
    )
    return _derived_result(
        destination,
        mime_type="image/jpeg",
        stream_index=video_stream_index,
        frame_index=frame_index,
        source_start_seconds=timestamp,
        source_end_seconds=timestamp,
        metadata={
            "metadata_stripped": True,
            "jpeg_quality": jpeg_quality,
            "source_stream_index": (
                video_stream_index
                if video_stream_index is not None
                else "not_available"
            ),
            "selection_index": frame_index,
            "selection_requested_timestamp_seconds": timestamp,
            "frame_index_semantics": (
                "representative_selection_ordinal_not_decoded_source_frame_index"
            ),
            "source_offset_semantics": "requested_seek_time_not_decoded_source_pts",
            "decoded_source_pts": "not_available",
            "decoded_source_timestamp_seconds": "not_available",
            "decoded_source_frame_index": "not_available",
            "decoded_coordinate_limitation": (
                "bounded_single_frame_extraction_does_not_expose_absolute_"
                "decoded_source_coordinates"
            ),
        },
    )


def extract_representative_frames(
    source_path: str | PathLike[str],
    destinations: Sequence[str | PathLike[str]],
    *,
    duration_seconds: float,
    maximum_frames: int,
    scene_change_times: Sequence[float] = (),
    video_stream_index: int | None = None,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> tuple[DerivedMediaFile, ...]:
    """Extract caller-provided representative-frame destinations in order."""
    times = representative_frame_times(
        duration_seconds,
        maximum_frames=maximum_frames,
        scene_change_times=scene_change_times,
    )
    if len(destinations) != len(times):
        raise ValueError("destinations must exactly match the selected frame count")
    normalized_destinations = [os.path.abspath(value) for value in destinations]
    if len(set(normalized_destinations)) != len(normalized_destinations):
        raise ValueError("frame destinations must be unique")
    for destination in destinations:
        _validated_output(source_path, destination)

    return tuple(
        extract_video_frame(
            source_path,
            destination,
            timestamp_seconds=timestamp,
            frame_index=index,
            duration_seconds=duration_seconds,
            video_stream_index=video_stream_index,
            jpeg_quality=jpeg_quality,
            ffmpeg_binary=ffmpeg_binary,
            runner=runner,
        )
        for index, (timestamp, destination) in enumerate(
            zip(times, destinations, strict=True)
        )
    )


def extract_normalized_audio_stream(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
    *,
    stream_index: int,
    duration_seconds: float,
    sample_rate: int = 16_000,
    channels: int = 1,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> DerivedMediaFile:
    """Extract one absolute stream index as metadata-free lossless FLAC."""
    _, destination = _validated_output(source_path, destination_path)
    if stream_index < 0:
        raise ValueError("stream_index cannot be negative")
    if sample_rate <= 0 or channels <= 0:
        raise ValueError("sample_rate and channels must be positive")
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration_seconds must be finite and non-negative")

    _run_to_new_file(
        runner,
        (
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
            "-map",
            f"0:{stream_index}",
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
            destination,
        ),
        destination=destination,
        timeout_seconds=ffmpeg_timeout_seconds(duration),
    )
    return _derived_result(
        destination,
        mime_type="audio/flac",
        stream_index=stream_index,
        source_start_seconds=0.0,
        source_end_seconds=duration,
        metadata={
            "metadata_stripped": True,
            "sample_rate": sample_rate,
            "channels": channels,
            "codec": "flac",
            "resampling": f"{sample_rate}_hz",
            "downmix": "mono" if channels == 1 else f"{channels}_channels",
        },
    )


def extract_faithful_audio_stream(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
    *,
    stream_index: int,
    duration_seconds: float,
    codec_name: str | None,
    sample_rate: int | None = None,
    channels: int | None = None,
    channel_layout: str | None = None,
    sample_format: str | None = None,
    bits_per_raw_sample: int | None = None,
    profile: str | None = None,
    codec_tag: str | None = None,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> DerivedMediaFile:
    """Extract one source-faithful audio stream without analysis normalization."""
    _, destination = _validated_output(source_path, destination_path)
    if stream_index < 0:
        raise ValueError("stream_index cannot be negative")
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration_seconds must be finite and non-negative")
    if sample_rate is not None and sample_rate <= 0:
        raise ValueError("sample_rate must be positive when provided")
    if channels is not None and channels <= 0:
        raise ValueError("channels must be positive when provided")

    output = faithful_audio_output_for_codec(codec_name)
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
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:a",
        "copy" if output.stream_copy else output.output_codec,
    ]
    if not output.stream_copy:
        if channels is not None:
            argv.extend(("-ac", str(channels)))
        if sample_rate is not None:
            argv.extend(("-ar", str(sample_rate)))
    argv.extend(
        (
            "-f",
            output.container,
            "-threads",
            "1",
            "-n",
            destination,
        )
    )
    _run_to_new_file(
        runner,
        tuple(argv),
        destination=destination,
        timeout_seconds=ffmpeg_timeout_seconds(duration),
    )
    normalized_codec = (
        codec_name.casefold().strip() if isinstance(codec_name, str) else None
    )
    return _derived_result(
        destination,
        mime_type=output.mime_type,
        stream_index=stream_index,
        source_start_seconds=0.0,
        source_end_seconds=duration,
        metadata={
            "metadata_stripped": True,
            "derivative_purpose": "source_faithful_audio_stream",
            "source_stream_index": stream_index,
            "source_codec": normalized_codec or "not_available",
            "source_profile": profile or "not_available",
            "source_codec_tag": codec_tag or "not_available",
            "source_sample_rate": sample_rate or "not_available",
            "source_channels": channels or "not_available",
            "source_channel_layout": channel_layout or "not_available",
            "source_sample_format": sample_format or "not_available",
            "source_bits_per_raw_sample": bits_per_raw_sample or "not_available",
            "extraction_mode": (
                "stream_copy" if output.stream_copy else "lossless_transcode"
            ),
            "packet_copy": output.stream_copy,
            "lossless_transcode": not output.stream_copy,
            "output_codec": output.output_codec,
            "output_container": output.container,
            "sample_rate_preservation": (
                "packet_copy"
                if output.stream_copy
                else (
                    "explicit_source_value"
                    if sample_rate is not None
                    else "ffmpeg_native_default_source_value_unavailable"
                )
            ),
            "channel_preservation": (
                "packet_copy"
                if output.stream_copy
                else (
                    "explicit_source_count"
                    if channels is not None
                    else "ffmpeg_native_default_source_value_unavailable"
                )
            ),
            "reencoding_loss": "none",
        },
    )


def extract_normalized_audio_chunk(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
    *,
    stream_index: int,
    start_seconds: float,
    end_seconds: float,
    overlap_before_seconds: float = 0.0,
    sample_rate: int = 16_000,
    channels: int = 1,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> DerivedMediaFile:
    """Extract one metadata-free FLAC chunk with exact traceable offsets."""
    _, destination = _validated_output(source_path, destination_path)
    overlap = float(overlap_before_seconds)
    if not math.isfinite(overlap) or overlap < 0:
        raise ValueError("overlap_before_seconds must be finite and non-negative")
    fingerprint = extract_audio_chunk(
        source_path,
        destination,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        stream_index=stream_index,
        sample_rate=sample_rate,
        channels=channels,
        ffmpeg_binary=ffmpeg_binary,
        runner=runner,
    )
    return DerivedMediaFile(
        path=destination,
        mime_type="audio/flac",
        sha256=fingerprint.sha256,
        size_bytes=fingerprint.size_bytes,
        stream_index=stream_index,
        source_start_seconds=float(start_seconds),
        source_end_seconds=float(end_seconds),
        metadata={
            "metadata_stripped": True,
            "sample_rate": sample_rate,
            "channels": channels,
            "codec": "flac",
            "resampling": f"{sample_rate}_hz",
            "downmix": "mono" if channels == 1 else f"{channels}_channels",
            "overlap_before_seconds": overlap,
        },
    )


def artwork_output_for_codec(codec_name: str) -> tuple[str, str, bool]:
    """Return extension, MIME type, and whether an artwork stream can be copied.

    Common still-image codecs are copied without re-encoding. Less common video
    codecs used as attached pictures are decoded to a metadata-free lossless PNG
    so downstream local image tools receive a bounded, conventional image file.
    """
    normalized = codec_name.casefold().strip()
    selected = _ARTWORK_CODECS.get(normalized)
    if selected is not None:
        return (*selected, True)
    return ("png", "image/png", False)


def attached_picture_pixel_preflight(
    stream: Mapping[str, Any],
    *,
    maximum_pixels: int,
) -> dict[str, Any]:
    """Classify declared attached-picture dimensions before FFmpeg decoding."""
    if maximum_pixels <= 0:
        raise ValueError("maximum_pixels must be positive")
    width = stream.get("width")
    height = stream.get("height")
    width = width if isinstance(width, int) and not isinstance(width, bool) else None
    height = (
        height if isinstance(height, int) and not isinstance(height, bool) else None
    )
    if width is None or height is None or width <= 0 or height <= 0:
        return {
            "status": "not_available",
            "declared_width": width if width is not None and width > 0 else None,
            "declared_height": height if height is not None and height > 0 else None,
            "declared_pixel_count": None,
            "maximum_pixels": maximum_pixels,
            "limitation": (
                "ffprobe_did_not_report_complete_positive_attached_picture_"
                "dimensions; bounded_post_extraction_pixel_preflight_required"
            ),
        }
    pixel_count = width * height
    return {
        "status": (
            "within_limit" if pixel_count <= maximum_pixels else "exceeds_limit"
        ),
        "declared_width": width,
        "declared_height": height,
        "declared_pixel_count": pixel_count,
        "maximum_pixels": maximum_pixels,
        "limitation": None,
    }


def embedded_artwork_streams_from_probe(
    streams: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Select FFprobe attached-picture streams without treating them as video."""
    selected = []
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        disposition = stream.get("disposition")
        if not isinstance(disposition, Mapping) or not disposition.get("attached_pic"):
            continue
        stream_index = stream.get("index")
        if not isinstance(stream_index, int) or stream_index < 0:
            continue
        codec_name = stream.get("codec_name")
        codec_name = codec_name if isinstance(codec_name, str) else "unknown"
        extension, mime_type, stream_copy = artwork_output_for_codec(codec_name)
        tags = stream.get("tags")
        tags = tags if isinstance(tags, Mapping) else {}
        selected.append(
            {
                "stream_index": stream_index,
                "source_codec": codec_name,
                "extension": extension,
                "mime_type": mime_type,
                "stream_copy": stream_copy,
                "width": stream.get("width"),
                "height": stream.get("height"),
                "language": tags.get("language"),
                "title": tags.get("title"),
                "comment": tags.get("comment"),
            }
        )
    return tuple(selected)


def extract_embedded_artwork(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
    *,
    stream_index: int,
    codec_name: str,
    duration_seconds: float,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> DerivedMediaFile:
    """Extract one attached picture as a traceable local-only derivative."""
    _, destination = _validated_output(source_path, destination_path)
    if stream_index < 0:
        raise ValueError("stream_index cannot be negative")
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration_seconds must be finite and non-negative")
    extension, mime_type, stream_copy = artwork_output_for_codec(codec_name)
    codec_arguments = ("-c:v", "copy") if stream_copy else ("-c:v", "png")
    _run_to_new_file(
        runner,
        (
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
            "-map",
            f"0:{stream_index}",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            *codec_arguments,
            "-f",
            "image2",
            "-threads",
            "1",
            "-n",
            destination,
        ),
        destination=destination,
        timeout_seconds=ffmpeg_timeout_seconds(duration),
    )
    return _derived_result(
        destination,
        mime_type=mime_type,
        stream_index=stream_index,
        metadata={
            "attached_picture": True,
            "source_codec": codec_name,
            "extraction_mode": "stream_copy" if stream_copy else "lossless_png",
            "container_metadata_stripped": True,
            "metadata_stripped": not stream_copy,
            "output_extension": extension,
        },
    )


def subtitle_output_for_codec(codec_name: str) -> tuple[str, str] | None:
    """Return a lossless text format and MIME type for supported subtitles."""
    return _SUBTITLE_CODECS.get(codec_name.casefold())


def extract_subtitle_stream(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
    *,
    stream_index: int,
    codec_name: str,
    duration_seconds: float,
    language: str | None = None,
    title: str | None = None,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., CommandResult] = run_command,
) -> DerivedMediaFile | None:
    """Extract a text subtitle stream, returning ``None`` for bitmap codecs."""
    output = subtitle_output_for_codec(codec_name)
    if output is None:
        return None
    _, destination = _validated_output(source_path, destination_path)
    if stream_index < 0:
        raise ValueError("stream_index cannot be negative")
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration_seconds must be finite and non-negative")
    output_format, mime_type = output
    output_codec = _SUBTITLE_ENCODERS[codec_name.casefold()]

    _run_to_new_file(
        runner,
        (
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
            "-map",
            f"0:{stream_index}",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:s",
            output_codec,
            "-f",
            output_format,
            "-threads",
            "1",
            "-n",
            destination,
        ),
        destination=destination,
        timeout_seconds=ffmpeg_timeout_seconds(duration),
    )
    return _derived_result(
        destination,
        mime_type=mime_type,
        stream_index=stream_index,
        source_start_seconds=0.0,
        source_end_seconds=duration,
        metadata={
            "metadata_stripped": True,
            "source_codec": codec_name,
            "language": language,
            "title": title,
        },
    )


def subtitle_streams_from_probe(
    streams: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Select text subtitle streams from FFprobe's technical stream summary."""
    selected = []
    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "")
        output = subtitle_output_for_codec(codec)
        if output is None:
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), Mapping) else {}
        selected.append(
            {
                "stream_index": stream.get("index"),
                "source_codec": codec,
                "extension": output[0],
                "mime_type": output[1],
                "language": tags.get("language"),
                "title": tags.get("title"),
            }
        )
    return tuple(selected)

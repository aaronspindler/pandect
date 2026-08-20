"""Fast integration coverage for the supported local media binaries.

The ordinary extractor tests use fixed tool responses so their assertions can
remain exact.  This module complements them with tiny generated media and
version-tolerant assertions against the real FFmpeg, FFprobe, ExifTool, and
Tesseract executables installed in the supported tools image.
"""

import math
import os
import shutil
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageDraw, ImageFont

from pandect.catalog.models import CatalogItem
from pandect.catalog.services import inspect_uploaded_media
from pandect.processing.extractors.audio import analyze_acoustics, detect_silence
from pandect.processing.extractors.derived_media import (
    detect_scene_changes,
    embedded_artwork_streams_from_probe,
    extract_embedded_artwork,
    extract_normalized_audio_chunk,
    extract_normalized_audio_stream,
    extract_subtitle_stream,
    extract_video_frame,
    subtitle_streams_from_probe,
)
from pandect.processing.extractors.errors import ToolFailedError
from pandect.processing.extractors.exiftool import extract_metadata
from pandect.processing.extractors.ffprobe import FFProbeMetadata, probe_media
from pandect.processing.extractors.hashing import fingerprint_file
from pandect.processing.extractors.images import analyze_image
from pandect.processing.extractors.subprocesses import run_command
from pandect.processing.extractors.tesseract import extract_ocr
from pandect.processing.extractors.timestamps import timestamp_claims_from_tags

_TOOLS_IMAGE_VENV = Path("/opt/pandect-venv")


def _required_binary(name: str) -> str:
    """Skip unsupported host runs, but make a missing tools-image binary fail."""
    binary = shutil.which(name)
    if binary is not None:
        return binary
    if _TOOLS_IMAGE_VENV.is_dir():
        pytest.fail(f"the supported tools image is missing required binary: {name}")
    pytest.skip(f"local integration binary is unavailable: {name}")


def _run_ffmpeg(ffmpeg: str, *arguments: str | os.PathLike[str]) -> None:
    run_command(
        (
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *arguments,
        ),
        timeout_seconds=30,
    )


@dataclass(frozen=True)
class RealMediaFixtures:
    video_with_audio: Path
    video_without_audio: Path
    audio_mp4: Path
    matroska_with_subtitles: Path
    webm: Path
    avif_image: Path
    audio_with_artwork: Path
    corrupt: Path


@pytest.fixture(scope="session")
def real_media(tmp_path_factory) -> RealMediaFixtures:
    ffmpeg = _required_binary("ffmpeg")
    directory = tmp_path_factory.mktemp("real-media")
    video_with_audio = directory / "video-with-audio.mp4"
    video_without_audio = directory / "video-without-audio.mp4"
    audio_mp4 = directory / "audio-only.m4a"
    matroska = directory / "subtitles-and-chapter.mkv"
    webm = directory / "video.webm"
    avif_image = directory / "image.avif"
    audio_with_artwork = directory / "audio-with-artwork.m4a"
    corrupt = directory / "corrupt.media"

    _run_ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=96x64:rate=5:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000:duration=1",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "mpeg4",
        "-q:v",
        "12",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "32k",
        "-threads",
        "1",
        "-shortest",
        "-movflags",
        "+faststart",
        "-metadata",
        "creation_time=2026-08-12T12:00:00Z",
        "-n",
        video_with_audio,
    )
    _run_ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:size=96x64:rate=5:duration=1",
        "-an",
        "-c:v",
        "mpeg4",
        "-q:v",
        "12",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "1",
        "-n",
        video_without_audio,
    )
    _run_ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:sample_rate=16000:duration=1",
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "32k",
        "-threads",
        "1",
        "-f",
        "ipod",
        "-n",
        audio_mp4,
    )
    _run_ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=96x64:rate=5:duration=1",
        "-an",
        "-c:v",
        "libvpx",
        "-deadline",
        "realtime",
        "-cpu-used",
        "8",
        "-b:v",
        "80k",
        "-threads",
        "1",
        "-f",
        "webm",
        "-n",
        webm,
    )

    _run_ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        "color=c=teal:size=48x32:rate=1:duration=1",
        "-frames:v",
        "1",
        "-c:v",
        "libaom-av1",
        "-still-picture",
        "1",
        "-threads",
        "1",
        "-f",
        "avif",
        "-n",
        avif_image,
    )
    cover = directory / "cover.jpg"
    Image.new("RGB", (48, 32), "#cc5500").save(cover, format="JPEG", quality=90)
    _run_ffmpeg(
        ffmpeg,
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=550:sample_rate=16000:duration=1",
        "-i",
        cover,
        "-map",
        "0:a:0",
        "-map",
        "1:v:0",
        "-c:a",
        "aac",
        "-b:a",
        "32k",
        "-c:v",
        "copy",
        "-disposition:v:0",
        "attached_pic",
        "-threads",
        "1",
        "-f",
        "ipod",
        "-n",
        audio_with_artwork,
    )

    captions = directory / "captions.srt"
    captions.write_text(
        "1\n00:00:00,100 --> 00:00:00,800\nPandect caption\n",
        encoding="utf-8",
    )
    chapters = directory / "chapters.ffmetadata"
    chapters.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=900\ntitle=Opening\n",
        encoding="utf-8",
    )
    _run_ffmpeg(
        ffmpeg,
        "-i",
        video_without_audio,
        "-f",
        "srt",
        "-i",
        captions,
        "-f",
        "ffmetadata",
        "-i",
        chapters,
        "-map",
        "0:v:0",
        "-map",
        "1:s:0",
        "-map_metadata",
        "2",
        "-map_chapters",
        "2",
        "-c:v",
        "copy",
        "-c:s",
        "srt",
        "-metadata:s:s:0",
        "language=eng",
        "-threads",
        "1",
        "-n",
        matroska,
    )
    corrupt.write_bytes(b"\x00not-a-real-media-container\xff")
    return RealMediaFixtures(
        video_with_audio=video_with_audio,
        video_without_audio=video_without_audio,
        audio_mp4=audio_mp4,
        matroska_with_subtitles=matroska,
        webm=webm,
        avif_image=avif_image,
        audio_with_artwork=audio_with_artwork,
        corrupt=corrupt,
    )


@pytest.fixture(scope="session")
def exif_jpeg(tmp_path_factory) -> Path:
    exiftool = _required_binary("exiftool")
    path = tmp_path_factory.mktemp("real-image") / "timestamp-and-gps.jpg"
    Image.new("RGB", (40, 30), "#336699").save(path, format="JPEG", quality=90)
    run_command(
        (
            exiftool,
            "-overwrite_original",
            "-EXIF:DateTimeOriginal=2026:08:12 10:11:12",
            "-EXIF:OffsetTimeOriginal=-04:00",
            "-EXIF:GPSLatitude=43.6533333333",
            "-EXIF:GPSLatitudeRef=N",
            "-EXIF:GPSLongitude=79.375",
            "-EXIF:GPSLongitudeRef=W",
            "-EXIF:GPSAltitude=75.5",
            "-EXIF:GPSAltitudeRef=0",
            "--",
            path,
        ),
        timeout_seconds=30,
    )
    return path


@pytest.fixture(scope="session")
def ocr_image(tmp_path_factory) -> Path:
    _required_binary("tesseract")
    path = tmp_path_factory.mktemp("real-ocr") / "ocr.png"
    image = Image.new("RGB", (900, 220), "white")
    drawing = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=58)
    drawing.text((35, 35), "PANDECT EVIDENCE 42", fill="black", font=font)
    drawing.text((35, 120), "LOCAL FORENSICS", fill="black", font=font)
    image.save(path, format="PNG")
    return path


@pytest.fixture(scope="session")
def wav_with_silence(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("real-audio") / "silence-tone-silence.wav"
    sample_rate = 16_000
    sample_count = 2 * sample_rate
    values = []
    for index in range(sample_count):
        seconds = index / sample_rate
        value = 0
        if 0.6 <= seconds < 1.4:
            value = round(0.45 * 32767 * math.sin(2 * math.pi * 440 * seconds))
        values.append(struct.pack("<h", value))
    with wave.open(os.fspath(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"".join(values))
    return path


def _stream_types(metadata: FFProbeMetadata) -> list[str]:
    return [str(stream["codec_type"]) for stream in metadata.streams]


def _raw_tag(metadata, suffix: str):
    return next(
        value
        for key, value in metadata.raw_tags.items()
        if key.rsplit(":", 1)[-1] == suffix
    )


def test_real_ffprobe_distinguishes_supported_containers_and_streams(real_media):
    _required_binary("ffprobe")

    audiovisual = probe_media(real_media.video_with_audio)
    silent_video = probe_media(real_media.video_without_audio)
    audio_only = probe_media(real_media.audio_mp4)
    matroska = probe_media(real_media.matroska_with_subtitles)
    webm = probe_media(real_media.webm)

    assert _stream_types(audiovisual) == ["video", "audio"]
    assert _stream_types(silent_video) == ["video"]
    assert _stream_types(audio_only) == ["audio"]
    assert audiovisual.streams[0]["width"] == 96
    assert audiovisual.streams[0]["height"] == 64
    assert audio_only.streams[0]["sample_rate"] == 16000
    assert len(matroska.chapters) == 1
    assert matroska.chapters[0]["tags"]["title"] == "Opening"
    assert any(stream["codec_type"] == "subtitle" for stream in matroska.streams)
    assert webm.streams[0]["codec_name"] == "vp8"
    assert matroska.streams[0]["codec_name"] == "mpeg4"
    assert 0.8 <= audiovisual.technical["format"]["duration_seconds"] <= 1.2


def test_real_intake_classification_uses_stream_truth_for_ambiguous_media(real_media):
    cases = (
        (
            real_media.video_with_audio,
            CatalogItem.ItemType.VIDEO,
            "video/mp4",
        ),
        (real_media.audio_mp4, CatalogItem.ItemType.AUDIO, "audio/mp4"),
        (
            real_media.matroska_with_subtitles,
            CatalogItem.ItemType.VIDEO,
            "video/x-matroska",
        ),
        (real_media.webm, CatalogItem.ItemType.VIDEO, "video/webm"),
        (real_media.avif_image, CatalogItem.ItemType.IMAGE, "image/avif"),
    )

    for path, expected_type, expected_mime in cases:
        upload = SimpleUploadedFile(path.name, path.read_bytes())
        upload.seek(7)
        inspection = inspect_uploaded_media(upload)
        assert inspection.item_type == expected_type
        assert inspection.mime_type == expected_mime
        assert upload.tell() == 7


def test_real_avif_and_attached_artwork_are_decoded_as_bounded_images(
    real_media, tmp_path
):
    avif_probe = probe_media(real_media.avif_image)
    avif = analyze_image(real_media.avif_image)
    artwork_probe = probe_media(real_media.audio_with_artwork)
    artwork_streams = embedded_artwork_streams_from_probe(artwork_probe.streams)

    assert avif_probe.streams[0]["codec_name"] == "av1"
    assert avif.technical["format"] == "AVIF"
    assert avif.technical["width"] == 48
    assert avif.technical["height"] == 32
    assert len(artwork_streams) == 1
    stream = artwork_streams[0]
    assert stream["width"] == 48
    assert stream["height"] == 32

    derived = extract_embedded_artwork(
        real_media.audio_with_artwork,
        tmp_path / f"artwork.{stream['extension']}",
        stream_index=stream["stream_index"],
        codec_name=stream["source_codec"],
        duration_seconds=1,
    )
    decoded = analyze_image(derived.path)
    assert derived.metadata["attached_picture"] is True
    assert derived.metadata["container_metadata_stripped"] is True
    assert decoded.technical["width"] == 48
    assert decoded.technical["height"] == 32


def test_real_exiftool_extracts_namespaced_timestamp_and_gps(exif_jpeg):
    before = fingerprint_file(exif_jpeg)
    metadata = extract_metadata(exif_jpeg)
    after = fingerprint_file(exif_jpeg)

    assert after == before
    assert _raw_tag(metadata, "DateTimeOriginal") == "2026:08:12 10:11:12"
    assert _raw_tag(metadata, "OffsetTimeOriginal") == "-04:00"
    assert metadata.gps == {
        "latitude": pytest.approx(43.6533333333, abs=1e-5),
        "longitude": pytest.approx(-79.375, abs=1e-5),
        "altitude_metres": pytest.approx(75.5, abs=0.1),
    }
    claims = timestamp_claims_from_tags(metadata.raw_tags)
    original = next(claim for claim in claims if claim.tag == "DateTimeOriginal")
    assert original.timezone_evidence == "explicit_offset"
    assert original.parsed_value.utcoffset().total_seconds() == -4 * 60 * 60
    assert metadata.local_path_tags_redacted is True


def test_real_tesseract_returns_text_geometry_and_confidence(ocr_image):
    metadata = extract_ocr(
        ocr_image,
        page_segmentation_mode=6,
        include_orientation=False,
    )
    normalized = " ".join(metadata.text.upper().split())

    assert "PANDECT" in normalized
    assert "EVIDENCE" in normalized
    assert metadata.page_count == 1
    assert metadata.words
    assert metadata.lines
    assert metadata.mean_confidence is not None
    assert 0 <= metadata.mean_confidence <= 100
    assert all(word["width"] > 0 and word["height"] > 0 for word in metadata.words)


def test_real_ffmpeg_audio_analysis_and_traceable_chunk(wav_with_silence, tmp_path):
    _required_binary("ffmpeg")
    _required_binary("ffprobe")
    before = fingerprint_file(wav_with_silence)

    silences = detect_silence(
        wav_with_silence,
        duration_seconds=2.0,
        noise_threshold_db=-35,
        minimum_duration_seconds=0.3,
    )
    acoustics = analyze_acoustics(wav_with_silence, duration_seconds=2.0)
    chunk = extract_normalized_audio_chunk(
        wav_with_silence,
        tmp_path / "chunk.flac",
        stream_index=0,
        start_seconds=0.5,
        end_seconds=1.5,
        overlap_before_seconds=0.1,
    )
    chunk_probe = probe_media(chunk.path)

    assert fingerprint_file(wav_with_silence) == before
    assert any(
        interval.start_seconds <= 0.05 and interval.end_seconds >= 0.5
        for interval in silences
    )
    assert any(
        interval.start_seconds <= 1.5 and interval.end_seconds >= 1.95
        for interval in silences
    )
    assert acoustics.volume["mean_volume_dbfs"] is not None
    assert acoustics.volume["max_sample_peak_dbfs"] is not None
    assert acoustics.loudness["integrated_loudness_lufs"] is not None
    assert chunk.sha256 == fingerprint_file(chunk.path).sha256
    assert chunk.size_bytes > 0
    assert chunk.source_start_seconds == 0.5
    assert chunk.source_end_seconds == 1.5
    assert chunk.metadata["overlap_before_seconds"] == 0.1
    assert chunk_probe.streams[0]["codec_name"] == "flac"
    assert chunk_probe.streams[0]["sample_rate"] == 16000
    assert chunk_probe.streams[0]["channels"] == 1


def test_real_ffmpeg_video_derivatives_and_subtitle_extraction(real_media, tmp_path):
    _required_binary("ffmpeg")
    _required_binary("ffprobe")
    original = fingerprint_file(real_media.video_with_audio)
    metadata = probe_media(real_media.video_with_audio)
    duration = metadata.technical["format"]["duration_seconds"]
    video_index = next(
        stream["index"]
        for stream in metadata.streams
        if stream["codec_type"] == "video"
    )
    audio_index = next(
        stream["index"]
        for stream in metadata.streams
        if stream["codec_type"] == "audio"
    )

    frame = extract_video_frame(
        real_media.video_with_audio,
        tmp_path / "frame.jpg",
        timestamp_seconds=0.2,
        frame_index=0,
        duration_seconds=duration,
        video_stream_index=video_index,
    )
    audio = extract_normalized_audio_stream(
        real_media.video_with_audio,
        tmp_path / "audio.flac",
        stream_index=audio_index,
        duration_seconds=duration,
    )
    frame_probe = probe_media(frame.path)
    audio_probe = probe_media(audio.path)

    subtitle_source = probe_media(real_media.matroska_with_subtitles)
    subtitle = subtitle_streams_from_probe(subtitle_source.streams)[0]
    extracted_subtitle = extract_subtitle_stream(
        real_media.matroska_with_subtitles,
        tmp_path / "caption.srt",
        stream_index=subtitle["stream_index"],
        codec_name=subtitle["source_codec"],
        duration_seconds=subtitle_source.technical["format"]["duration_seconds"],
        language=subtitle["language"],
        title=subtitle["title"],
    )

    assert fingerprint_file(real_media.video_with_audio) == original
    assert frame.mime_type == "image/jpeg"
    assert frame.size_bytes > 0
    assert frame.metadata["metadata_stripped"] is True
    assert frame_probe.streams[0]["width"] == 96
    assert frame_probe.streams[0]["height"] == 64
    assert audio.mime_type == "audio/flac"
    assert audio.source_start_seconds == 0.0
    assert audio.source_end_seconds == duration
    assert audio_probe.streams[0]["sample_rate"] == 16000
    assert audio_probe.streams[0]["channels"] == 1
    assert extracted_subtitle is not None
    assert extracted_subtitle.mime_type == "application/x-subrip"
    assert "Pandect caption" in Path(extracted_subtitle.path).read_text(
        encoding="utf-8"
    )


def test_real_ffmpeg_scene_detection_returns_ranked_scores(real_media):
    ffmpeg = _required_binary("ffmpeg")
    metadata = probe_media(real_media.video_with_audio)
    duration = metadata.technical["format"]["duration_seconds"]
    video_index = next(
        stream["index"]
        for stream in metadata.streams
        if stream["codec_type"] == "video"
    )

    changes = detect_scene_changes(
        real_media.video_with_audio,
        duration_seconds=duration,
        threshold=0.000001,
        video_stream_index=video_index,
        ffmpeg_binary=ffmpeg,
    )

    assert changes
    assert all(0 <= change.timestamp_seconds < duration for change in changes)
    assert all(0 <= change.score <= 1 for change in changes)
    assert list(changes) == sorted(
        changes,
        key=lambda change: (-change.score, change.timestamp_seconds),
    )


def test_real_tools_reject_corrupt_media_without_creating_results(real_media):
    _required_binary("ffprobe")
    _required_binary("exiftool")

    with pytest.raises(ToolFailedError):
        probe_media(real_media.corrupt)
    with pytest.raises(ToolFailedError):
        extract_metadata(real_media.corrupt)

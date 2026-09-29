"""Synthetic, evidence-free smoke checks through the real isolated runner."""

import math
import os
import struct
import tempfile
import zlib
from pathlib import Path

from pandect.processing.extractors.audio import extract_audio_chunk
from pandect.processing.extractors.exiftool import extract_metadata
from pandect.processing.extractors.ffprobe import probe_media
from pandect.processing.extractors.hashing import fingerprint_file
from pandect.processing.extractors.images import analyze_image, render_image_frame
from pandect.processing.extractors.tesseract import extract_ocr

from .client import run_operation, status

IMAGE_WIDTH = 368
IMAGE_HEIGHT = 120
SAMPLE_RATE = 16_000


def synthetic_png():
    """Encode a code-owned bitmap directly, without loading a trusted decoder."""

    def chunk(kind, payload):
        return (
            struct.pack("!I", len(payload))
            + kind
            + payload
            + struct.pack("!I", zlib.crc32(kind + payload))
        )

    glyphs = {
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
        "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
        "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
        "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
        " ": ("00000",) * 7,
    }
    scale = 8
    rows = [bytearray(b"\xff\xff\xff" * IMAGE_WIDTH) for _ in range(IMAGE_HEIGHT)]
    for letter_index, letter in enumerate("TEST 42"):
        for y, row in enumerate(glyphs[letter]):
            for x, pixel in enumerate(row):
                if pixel != "1":
                    continue
                for offset in range(scale):
                    start = (16 + letter_index * 6 * scale + x * scale) * 3
                    rows[24 + y * scale + offset][start : start + scale * 3] = b"\0" * (
                        scale * 3
                    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR", struct.pack("!IIBBBBB", IMAGE_WIDTH, IMAGE_HEIGHT, 8, 2, 0, 0, 0)
        )
        + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in rows)))
        + chunk(b"IEND", b"")
    )


def synthetic_wav():
    """One second of synthetic 440 Hz mono PCM; no file or microphone input."""
    pcm = b"".join(
        struct.pack(
            "<h", round(4000 * math.sin(2 * math.pi * 440 * index / SAMPLE_RATE))
        )
        for index in range(SAMPLE_RATE)
    )
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + len(pcm),
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            SAMPLE_RATE,
            SAMPLE_RATE * 2,
            2,
            16,
            b"data",
            len(pcm),
        )
        + pcm
    )


def check_media(directory):
    """Use public adapters so real IPC, validators and returned files are tested."""
    image = directory / "synthetic.png"
    audio = directory / "synthetic.wav"
    image.write_bytes(synthetic_png())
    audio.write_bytes(synthetic_wav())
    originals = {path: fingerprint_file(path) for path in (image, audio)}

    image_metadata = analyze_image(image).technical
    if (image_metadata["width"], image_metadata["height"]) != (
        IMAGE_WIDTH,
        IMAGE_HEIGHT,
    ):
        raise RuntimeError("The isolated image geometry check failed.")
    tags = extract_metadata(image).raw_tags
    if not any(
        key.endswith(":ImageWidth") and value == IMAGE_WIDTH
        for key, value in tags.items()
    ):
        raise RuntimeError("The isolated ExifTool image check failed.")
    ocr = extract_ocr(image, page_segmentation_mode=7, include_orientation=False)
    if "TEST" not in ocr.text.upper() or not ocr.words:
        raise RuntimeError("The isolated synthetic OCR check failed.")

    rendered = directory / "rendered.png"
    render_result = render_image_frame(image, rendered)
    if render_result["width"] != IMAGE_WIDTH or not rendered.is_file():
        raise RuntimeError("The isolated image artifact transfer failed.")
    if analyze_image(rendered).technical["height"] != IMAGE_HEIGHT:
        raise RuntimeError("The isolated derived image recheck failed.")

    audio_metadata = probe_media(audio).technical
    stream = audio_metadata["streams"][0]
    if (
        stream["codec_type"] != "audio"
        or stream["sample_rate"] != SAMPLE_RATE
        or stream["channels"] != 1
        or not math.isclose(
            audio_metadata["format"]["duration_seconds"], 1, abs_tol=0.01
        )
    ):
        raise RuntimeError("The isolated synthetic WAV probe failed.")
    chunk = directory / "derived.flac"
    generated = extract_audio_chunk(audio, chunk, start_seconds=0.25, end_seconds=0.75)
    if generated != fingerprint_file(chunk):
        raise RuntimeError("The isolated audio artifact hash check failed.")
    derived = probe_media(chunk).technical
    if derived["streams"][0]["codec_name"] != "flac" or not math.isclose(
        derived["format"]["duration_seconds"], 0.5, abs_tol=0.01
    ):
        raise RuntimeError("The isolated derived audio recheck failed.")
    if any(fingerprint_file(path) != expected for path, expected in originals.items()):
        raise RuntimeError("The synthetic originals changed during processing.")


def main():
    state = status()
    if state["busy"]:
        raise SystemExit("Runner is busy; retry the synthetic smoke check later.")
    result = run_operation(
        "tool_version", inputs={}, parameters={"tool": "ffprobe"}, timeout_seconds=30
    )
    if not isinstance(result.get("version"), str):
        raise RuntimeError("The isolated FFprobe version check failed.")
    # Derivatives use the normal data volume's free-space guard, not the web
    # container's deliberately tiny /tmp. Only this private directory is removed.
    workspace = Path(os.environ.get("PANDECT_WORK_DIR", tempfile.gettempdir()))
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="pandect-runner-smoke-", dir=workspace
    ) as temporary:
        check_media(Path(temporary))
    print(
        "Media runner: peer-checked IPC, WAV/FLAC probes, image decode/render, "
        "ExifTool, OCR, FFmpeg audio extraction, artifact hashes, and unchanged "
        "synthetic originals passed."
    )


if __name__ == "__main__":
    main()

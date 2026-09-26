"""Bounded Pillow-based technical analysis for image evidence."""

import math
import statistics
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from math import gcd
from os import PathLike
from typing import Any

from pandect.media_runner.routing import isolated, require_child
from pandect.processing.extractors.errors import (
    OptionalDependencyUnavailableError,
    ResourceLimitError,
    ToolFailedError,
    UnsupportedPixelFormatError,
)

MAX_IMAGE_PIXELS = 200_000_000
MAX_DECODED_FRAMES = 100
MAX_TOTAL_DECODED_PIXELS = MAX_IMAGE_PIXELS
_HASH_SIZE = 8
_PHASH_SAMPLE_SIZE = 32
_IMAGE_SIGNATURE_BYTES = 512
_AVIF_BRANDS = {b"avif", b"avis"}
_HEIF_BRANDS = {
    b"heic",
    b"heix",
    b"heim",
    b"heis",
    b"hevc",
    b"hevx",
    b"mif1",
    b"msf1",
}


def _pillow():
    require_child()
    try:
        from PIL import Image, ImageFilter, ImageOps
    except ImportError as error:  # pragma: no cover - depends on runtime image.
        raise OptionalDependencyUnavailableError from error
    return Image, ImageFilter, ImageOps


def _unsupported_iso_bmff_decoder(path: str | PathLike[str], image: Any) -> bool:
    """Identify a complete HEIF/AVIF ``ftyp`` box lacking a Pillow decoder.

    ``Image.open`` uses the same exception for an unknown format and a damaged
    supported image.  Checking both the source bytes and Pillow's registered
    decoders keeps those cases separate: a corrupt PNG/JPEG remains a failure,
    while a valid locally unsupported ISO-BMFF image can be skipped explicitly.
    """
    try:
        with open(path, "rb") as source:
            data = source.read(_IMAGE_SIGNATURE_BYTES)
    except OSError:
        return False
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(data[:4], "big")
    if box_size < 16 or box_size > len(data):
        return False
    brands = {data[8:12]} | {
        data[position : position + 4] for position in range(16, box_size - 3, 4)
    }
    if brands & _AVIF_BRANDS:
        required_decoders = {"AVIF"}
    elif brands & _HEIF_BRANDS:
        required_decoders = {"HEIF", "HEIC"}
    else:
        return False

    # registered_extensions() initializes Pillow's built-in and installed
    # plugins without decoding the evidence.
    registered = {
        str(format_name).upper()
        for format_name in image.registered_extensions().values()
    }
    return not bool(required_decoders & registered)


def _open_image(image: Any, path: str | PathLike[str]) -> Any:
    """Open an image while preserving unsupported-vs-corrupt semantics."""
    try:
        return image.open(path)
    except (OSError, ValueError) as error:
        if _unsupported_iso_bmff_decoder(path, image):
            raise UnsupportedPixelFormatError from error
        raise ToolFailedError from error


def _mode_details(mode: str, bands: tuple[str, ...]) -> dict[str, Any]:
    bits_per_channel = 8
    if mode == "1":
        bits_per_channel = 1
    elif mode.startswith("I;16"):
        bits_per_channel = 16
    elif mode in {"I", "F"}:
        bits_per_channel = 32
    return {
        "mode": mode,
        "bands": list(bands),
        "channel_count": len(bands),
        "bits_per_channel": bits_per_channel,
    }


def _pixel_values(image: Any) -> Any:
    """Use Pillow's current flat accessor while supporting the locked range."""
    flattened = getattr(image, "get_flattened_data", None)
    return flattened() if flattened else image.getdata()


def _dhash(image: Any) -> str:
    resized = image.convert("L").resize((_HASH_SIZE + 1, _HASH_SIZE))
    pixels = list(_pixel_values(resized))
    value = 0
    for row in range(_HASH_SIZE):
        offset = row * (_HASH_SIZE + 1)
        for column in range(_HASH_SIZE):
            value = (value << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return f"{value:016x}"


def _phash(image: Any) -> str:
    pixels = list(
        _pixel_values(
            image.convert("L").resize((_PHASH_SAMPLE_SIZE, _PHASH_SAMPLE_SIZE))
        )
    )
    cosines = [
        [
            math.cos(
                (2 * coordinate + 1) * frequency * math.pi / (2 * _PHASH_SAMPLE_SIZE)
            )
            for coordinate in range(_PHASH_SAMPLE_SIZE)
        ]
        for frequency in range(_HASH_SIZE)
    ]
    coefficients: list[float] = []
    for vertical_frequency in range(_HASH_SIZE):
        for horizontal_frequency in range(_HASH_SIZE):
            coefficient = 0.0
            for y in range(_PHASH_SAMPLE_SIZE):
                vertical_weight = cosines[vertical_frequency][y]
                row = y * _PHASH_SAMPLE_SIZE
                coefficient += vertical_weight * sum(
                    pixels[row + x] * cosines[horizontal_frequency][x]
                    for x in range(_PHASH_SAMPLE_SIZE)
                )
            coefficients.append(coefficient)
    median = statistics.median(coefficients[1:])
    value = 0
    for coefficient in coefficients:
        value = (value << 1) | int(coefficient > median)
    return f"{value:016x}"


def _dominant_colors(image: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    sample = image.convert("RGBA")
    sample.thumbnail((128, 128))
    counter: Counter[tuple[int, int, int]] = Counter()
    for red, green, blue, alpha in _pixel_values(sample):
        if alpha == 0:
            continue
        # Four-bit channels suppress insignificant compression noise while
        # remaining deterministic across supported platforms.
        counter[(red // 16 * 17, green // 16 * 17, blue // 16 * 17)] += 1
    total = counter.total()
    if not total:
        return []
    return [
        {
            "hex": f"#{red:02x}{green:02x}{blue:02x}",
            "proportion": round(count / total, 6),
        }
        for (red, green, blue), count in counter.most_common(limit)
    ]


def _quality_statistics(image: Any, image_filter: Any) -> dict[str, float]:
    grayscale = image.convert("L")
    grayscale.thumbnail((256, 256))
    histogram = grayscale.histogram()
    total = sum(histogram)
    if not total:
        return {
            "mean": 0.0,
            "standard_deviation": 0.0,
            "shadow_clip_proportion": 0.0,
            "highlight_clip_proportion": 0.0,
            "entropy_bits": 0.0,
            "edge_standard_deviation": 0.0,
        }
    mean = sum(level * count for level, count in enumerate(histogram)) / total
    variance = (
        sum(((level - mean) ** 2) * count for level, count in enumerate(histogram))
        / total
    )
    probabilities = [count / total for count in histogram if count]
    edges = grayscale.filter(image_filter.FIND_EDGES)
    edge_histogram = edges.histogram()
    edge_total = sum(edge_histogram)
    edge_mean = (
        sum(level * count for level, count in enumerate(edge_histogram)) / edge_total
    )
    edge_variance = (
        sum(
            ((level - edge_mean) ** 2) * count
            for level, count in enumerate(edge_histogram)
        )
        / edge_total
    )
    return {
        "mean": round(mean, 6),
        "standard_deviation": round(math.sqrt(variance), 6),
        "shadow_clip_proportion": round(sum(histogram[:6]) / total, 6),
        "highlight_clip_proportion": round(sum(histogram[250:]) / total, 6),
        "entropy_bits": round(
            -sum(value * math.log2(value) for value in probabilities), 6
        ),
        "edge_standard_deviation": round(math.sqrt(edge_variance), 6),
    }


def _safe_info(info: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "background",
        "compression",
        "dpi",
        "duration",
        "jfif_density",
        "jfif_unit",
        "loop",
        "progression",
        "progressive",
        "transparency",
    }
    result: dict[str, Any] = {}
    for key in sorted(allowed.intersection(info)):
        value = info[key]
        if isinstance(value, (str, int, float, bool, type(None))):
            result[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(part, (str, int, float, bool, type(None))) for part in value
        ):
            result[key] = list(value)
    return result


@dataclass(frozen=True)
class ImageTechnicalMetadata:
    """JSON-compatible technical facts derived without changing the image."""

    technical: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return deepcopy(self.technical)


@isolated("analyze_image", source="path")
def analyze_image(
    path: str | PathLike[str],
    *,
    max_pixels: int = MAX_IMAGE_PIXELS,
    max_frames: int = MAX_DECODED_FRAMES,
    max_total_decoded_pixels: int | None = None,
) -> ImageTechnicalMetadata:
    """Decode bounded frames and calculate stable technical image features."""
    if max_total_decoded_pixels is None:
        # The configured pixel ceiling applies both to each individual unit
        # and to the aggregate work performed for an image. This prevents a
        # many-page document made of individually valid pages from multiplying
        # the decoder's memory and CPU budget without bound.
        max_total_decoded_pixels = max_pixels
    if min(max_pixels, max_frames, max_total_decoded_pixels) <= 0:
        raise ValueError("image resource limits must be positive")
    Image, ImageFilter, ImageOps = _pillow()

    previous_limit = Image.MAX_IMAGE_PIXELS
    # Pillow's warning/error threshold is intentionally non-configurable per
    # image. Disable that global threshold only while reading the header, then
    # enforce Pandect's stricter explicit limit before any frame is decoded.
    Image.MAX_IMAGE_PIXELS = None
    try:
        source = _open_image(Image, path)
        with source:
            width, height = source.size
            pixels_per_frame = width * height
            if pixels_per_frame > max_pixels:
                raise ResourceLimitError

            frame_count = int(getattr(source, "n_frames", 1) or 1)
            source_info = dict(source.info)
            source_mode = source.mode
            source_bands = tuple(source.getbands())
            orientation = None
            try:
                orientation = source.getexif().get(274)
            except (AttributeError, OSError, ValueError):
                pass
            decoded_frames: list[dict[str, Any]] = []
            decoded_pixels = 0
            truncation_reason = None
            truncation_frame_index = None
            for frame_index in range(min(frame_count, max_frames)):
                try:
                    source.seek(frame_index)
                except (EOFError, OSError, ValueError) as error:
                    raise ToolFailedError from error

                # Multi-page TIFF and similar containers may give every page a
                # different geometry. Inspect this unit's header after seek and
                # enforce both limits before copy/load expands compressed pixel
                # data. Never budget later pages using page zero's dimensions.
                frame_width, frame_height = source.size
                frame_pixels = frame_width * frame_height
                if frame_pixels > max_pixels:
                    raise ResourceLimitError
                if decoded_pixels + frame_pixels > max_total_decoded_pixels:
                    truncation_reason = "total_pixel_limit"
                    truncation_frame_index = frame_index
                    break
                try:
                    frame = ImageOps.exif_transpose(source.copy())
                    frame.load()
                except (EOFError, OSError, ValueError) as error:
                    raise ToolFailedError from error
                decoded_pixels += frame_pixels
                decoded_frames.append(
                    {
                        "index": frame_index,
                        "width": frame.width,
                        "height": frame.height,
                        "duration_milliseconds": source.info.get("duration"),
                        "dhash": _dhash(frame),
                        "phash": _phash(frame),
                        "dominant_colors": _dominant_colors(frame),
                        "quality": _quality_statistics(frame, ImageFilter),
                    }
                )

            if frame_count > max_frames and truncation_reason is None:
                truncation_reason = "frame_limit"
                truncation_frame_index = max_frames
            if not decoded_frames:
                raise ResourceLimitError

            profile = source_info.get("icc_profile")
            if isinstance(profile, str):
                profile = profile.encode("utf-8", errors="replace")
            profile_metadata = None
            if isinstance(profile, bytes):
                profile_metadata = {
                    "size_bytes": len(profile),
                    "sha256": sha256(profile).hexdigest(),
                }
            divisor = gcd(width, height) if width and height else 1

            return ImageTechnicalMetadata(
                {
                    "format": source.format,
                    "mime_type": Image.MIME.get(source.format),
                    "width": width,
                    "height": height,
                    "pixel_count": pixels_per_frame,
                    "aspect_ratio": (
                        f"{width // divisor}:{height // divisor}"
                        if width and height
                        else None
                    ),
                    **_mode_details(source_mode, source_bands),
                    "has_alpha": ("A" in source_bands or "transparency" in source_info),
                    "orientation": orientation,
                    "frame_count": frame_count,
                    "is_animated": bool(getattr(source, "is_animated", False)),
                    "frames_analyzed": len(decoded_frames),
                    "analysis_truncated": truncation_reason is not None,
                    "truncation_reason": truncation_reason,
                    "truncation_frame_index": truncation_frame_index,
                    "decoded_pixel_count": decoded_pixels,
                    "icc_profile": profile_metadata,
                    "container_info": _safe_info(source_info),
                    "dhash": decoded_frames[0]["dhash"],
                    "phash": decoded_frames[0]["phash"],
                    "dominant_colors": decoded_frames[0]["dominant_colors"],
                    "quality": decoded_frames[0]["quality"],
                    "frames": decoded_frames,
                }
            )
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


@isolated("render_image_frame", source="path", destination="destination")
def render_image_frame(
    path: str | PathLike[str],
    destination: str | PathLike[str],
    *,
    frame_index: int = 0,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> dict[str, Any]:
    """Render one bounded frame/page as a metadata-free PNG work image.

    The output is intentionally an ephemeral processor input, not evidence.
    Its single-page shape prevents tools such as Tesseract from independently
    traversing an unbounded multi-page source after Pandect's preflight.
    """
    if frame_index < 0:
        raise ValueError("frame_index must not be negative")
    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    Image, _ImageFilter, ImageOps = _pillow()
    output_path = str(destination)

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        source = _open_image(Image, path)
        with source:
            try:
                source.seek(frame_index)
            except (EOFError, OSError, ValueError) as error:
                raise ToolFailedError from error
            width, height = source.size
            pixel_count = width * height
            if pixel_count > max_pixels:
                raise ResourceLimitError
            try:
                frame = ImageOps.exif_transpose(source.copy())
                frame.load()
                has_alpha = "A" in frame.getbands() or "transparency" in frame.info
                if has_alpha:
                    target_mode = "RGBA"
                elif frame.mode in {"1", "L"}:
                    target_mode = "L"
                else:
                    target_mode = "RGB"
                converted = frame.convert(target_mode)
                frame.close()
                frame = converted
                # Pillow may otherwise carry EXIF, ICC, DPI, comments, or
                # container-specific fields from the source copy into PNG.
                frame.info.clear()
                with open(output_path, "xb") as output:
                    frame.save(output, format="PNG")
            except FileExistsError:
                raise
            except (EOFError, OSError, ValueError) as error:
                raise ToolFailedError from error
            finally:
                if "frame" in locals():
                    frame.close()
        return {
            "frame_index": frame_index,
            "width": width,
            "height": height,
            "pixel_count": pixel_count,
            "format": "PNG",
            "metadata_stripped": True,
        }
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

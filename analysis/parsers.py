"""Small bounded parser subprocesses; originals enter through verified bytes.

The child has no Django configuration, credentials, case records or storage paths.
It reads stdin and emits a bounded JSON result. This is resource isolation, not
an operating-system security sandbox; deployed workers still need confinement.
"""

import base64
import hashlib
import io
import json
import logging
import math
import os
import runpy
import struct
import subprocess
import sys
import time
import warnings
import wave
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_PAGES = 100
MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_AUDIO_SECONDS = 1800
PARSER_SECONDS = 20


class ParserFailure(Exception):
    pass


def read_source(source):
    """Read through storage, checking the applicable original file-byte hash."""
    from django.conf import settings

    from .errors import AnalysisError

    record = source.attachment if source.attachment is not None else source.item
    original = getattr(record, "file", None)
    if not original:
        raise AnalysisError("retained_file_required")
    maximum = min(MAX_BYTES, settings.PANDECT_ANALYSIS_MAX_BYTES)
    if getattr(record, "byte_size", maximum + 1) > maximum:
        raise AnalysisError("input_limit")
    expected = record.audio_sha256 if source.attachment is not None else record.sha256
    try:
        with original.storage.open(original.name, "rb") as stream:
            content = stream.read(maximum + 1)
    except OSError:
        raise AnalysisError("source_unavailable") from None
    if len(content) > maximum:
        raise AnalysisError("input_limit")
    if (
        len(content) != record.byte_size
        or hashlib.sha256(content).hexdigest() != expected
    ):
        raise AnalysisError("source_changed")
    return content, expected


def run_parser(operation, content, *, options=None):
    """Keep risky decoding outside the worker and bound wall time and output."""
    from .errors import AnalysisError

    if operation not in {
        "document",
        "image",
        "ocr_image",
        "audio",
        "audio_chunk",
        "geographic_context",
    }:
        raise AnalysisError("unsupported_parser")
    if not isinstance(content, bytes) or len(content) > MAX_BYTES:
        raise AnalysisError("input_limit")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                operation,
                json.dumps(options or {}),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"}
                },
                # Native geometry dependencies must not create an unbounded
                # numerical-library thread pool inside the bounded child.
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
            },
        )
    except OSError:
        raise AnalysisError("parser_unavailable") from None
    deadline = time.monotonic() + PARSER_SECONDS
    try:
        pending = content
        while True:
            try:
                stdout, _ = process.communicate(input=pending, timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pending = None
                if time.monotonic() > deadline:
                    raise AnalysisError("parser_timeout") from None
                if sys.platform == "darwin":
                    usage = subprocess.run(
                        ["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=1,
                        check=False,
                    )
                    if (
                        usage.returncode == 0
                        and int(usage.stdout.strip() or b"0") > 1024**2
                    ):
                        raise AnalysisError("parser_resource_limit") from None
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
    if process.returncode != 0 or len(stdout) > MAX_OUTPUT:
        raise AnalysisError("parser_resource_limit")
    try:
        value = json.loads(stdout)
    except ValueError, UnicodeError:
        raise AnalysisError("parser_failed") from None
    if not isinstance(value, dict):
        raise AnalysisError("parser_failed")
    if "error" in value:
        raise AnalysisError(value["error"])
    return value


def _safe_metadata(value, depth=0):
    if depth > 6:
        return "[nested metadata omitted]"
    if isinstance(value, str):
        if len(value) > 8192:
            return {
                "text_prefix": value[:8192],
                "character_count": len(value),
                "truncated": True,
            }
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, bytes):
        return {
            "bytes_hex": value[:8192].hex(),
            "byte_count": len(value),
            "truncated": len(value) > 8192,
        }
    if isinstance(value, dict):
        entries = {
            str(key)[:256]: _safe_metadata(item, depth + 1)
            for key, item in list(value.items())[:256]
        }
        return (
            {"entries": entries, "entry_count": len(value), "truncated": True}
            if len(value) > 256
            else entries
        )
    if isinstance(value, (tuple, list)):
        entries = [_safe_metadata(item, depth + 1) for item in value[:256]]
        return (
            {"entries": entries, "entry_count": len(value), "truncated": True}
            if len(value) > 256
            else entries
        )
    try:
        numeric = float(value)
        return numeric if math.isfinite(numeric) else str(value)[:256]
    except ValueError, TypeError, ZeroDivisionError, OverflowError:
        return str(value)[:256]


def _pdf(content):
    import pypdf
    from pypdf import PdfReader

    pypdf.overwrite_configuration(
        maximum_declared_stream_length=MAX_STREAM_BYTES,
        array_based_stream_maximum_output_length=MAX_STREAM_BYTES,
        zlib_maximum_output_length=MAX_STREAM_BYTES,
        lzw_maximum_output_length=MAX_STREAM_BYTES,
        run_length_maximum_output_length=MAX_STREAM_BYTES,
        brotli_maximum_output_length=MAX_STREAM_BYTES,
        stream_decoding_work_maximum_length=MAX_BYTES,
        stream_filters_maximum_length=8,
        page_tree_maximum_depth=20,
        jbig2dec_binary=None,
    )
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted:
        return {"status": "unsupported", "reason": "encrypted_pdf", "pages": []}
    page_count = len(reader.pages)
    if page_count > MAX_PAGES:
        raise ParserFailure("document_page_limit")
    pages = []
    text_bytes = 0
    stream_bytes = 0
    for number, page in enumerate(reader.pages, start=1):
        contents = page.get_contents()
        if contents is not None:
            size = len(contents.get_data())
            stream_bytes += size
            if size > MAX_STREAM_BYTES or stream_bytes > MAX_BYTES:
                raise ParserFailure("document_stream_limit")
        text = page.extract_text() or ""
        text_bytes += len(text.encode("utf-8"))
        if text_bytes > MAX_TEXT:
            raise ParserFailure("extracted_text_limit")
        pages.append({"page": number, "text": text, "native_text": bool(text.strip())})
    return {
        "status": "complete",
        "format": "PDF",
        "version": f"pypdf {pypdf.__version__}",
        "pages": pages,
        "page_count": page_count,
    }


def _document(content):
    if content.startswith(b"%PDF-"):
        from pypdf.errors import LimitReachedError

        try:
            return _pdf(content)
        except LimitReachedError:
            raise ParserFailure("document_stream_limit") from None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        return {"status": "unsupported", "reason": "unsupported_document_format"}
    if any(ord(character) < 32 and character not in "\t\r\n" for character in text):
        return {"status": "unsupported", "reason": "unsupported_document_format"}
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise ParserFailure("extracted_text_limit")
    return {
        "status": "complete",
        "format": "UTF-8",
        "version": f"Python {sys.version_info.major}.{sys.version_info.minor} UTF-8",
        "text": text,
        "line_count": len(text.splitlines()) or 1,
        "bom": content.startswith(b"\xef\xbb\xbf"),
    }


def _gps(gps):
    try:
        latitude_ref, longitude_ref = gps[1], gps[3]
        if latitude_ref not in {"N", "S"} or longitude_ref not in {"E", "W"}:
            return None
        positions = []
        for values, maximum in ((gps[2], 90), (gps[4], 180)):
            degree, minute, second = map(float, values)
            if not all(math.isfinite(value) for value in (degree, minute, second)):
                return None
            if not (0 <= degree <= maximum and 0 <= minute < 60 and 0 <= second < 60):
                return None
            number = degree + minute / 60 + second / 3600
            if number > maximum:
                return None
            positions.append(number)
        return [
            positions[0] * (-1 if latitude_ref == "S" else 1),
            positions[1] * (-1 if longitude_ref == "W" else 1),
        ]
    except KeyError, ValueError, TypeError, ZeroDivisionError:
        return None


def _image(content, *, prepare=False):
    import PIL
    from PIL import ExifTags, Image

    if not (
        content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        return {"status": "unsupported", "reason": "unsupported_image_format"}
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(io.BytesIO(content), formats=["JPEG", "PNG"]) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ParserFailure("image_pixel_limit")
        if getattr(image, "n_frames", 1) != 1:
            return {"status": "unsupported", "reason": "animated_image"}
        image.load()
        result = {
            "status": "complete",
            "version": f"Pillow {PIL.__version__}",
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
        }
        if prepare:
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", image.size, "white")
                prepared = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                prepared = image.convert("RGB")
            prepared.thumbnail((2048, 2048))
            buffer = io.BytesIO()
            prepared.save(buffer, format="JPEG", quality=90)
            encoded = buffer.getvalue()
            if len(encoded) > 4 * 1024 * 1024:
                raise ParserFailure("prepared_image_limit")
            result |= {
                "image": base64.b64encode(encoded).decode("ascii"),
                "prepared_width": prepared.width,
                "prepared_height": prepared.height,
            }
            return result
        exif = image.getexif()
        raw = {
            ExifTags.TAGS.get(key, str(key)): _safe_metadata(value)
            for key, value in exif.items()
        }
        exif_fields = exif.get_ifd(ExifTags.IFD.Exif)
        raw["ExifIFD"] = {
            ExifTags.TAGS.get(key, str(key)): _safe_metadata(value)
            for key, value in exif_fields.items()
        }
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        raw["GPSInfo"] = {
            ExifTags.GPSTAGS.get(key, str(key)): _safe_metadata(value)
            for key, value in gps.items()
        }
        xmp = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")
        if isinstance(xmp, bytes):
            if len(xmp) > MAX_METADATA_BYTES:
                raise ParserFailure("image_metadata_limit")
            xmp = xmp.decode("utf-8", errors="replace")
        if xmp:
            if not isinstance(xmp, str) or len(xmp.encode()) > MAX_METADATA_BYTES:
                raise ParserFailure("image_metadata_limit")
            raw["XMP"] = xmp
            if "<!doctype" in xmp.lower() or "<!entity" in xmp.lower():
                result["xmp_warning"] = (
                    "DTD/entity-bearing XMP was retained without interpretation."
                )
            else:
                try:
                    tree = ElementTree.fromstring(xmp)
                    result["xmp_fields"] = {
                        key: value[:8192]
                        for element in list(tree.iter())[:256]
                        for key, value in element.attrib.items()
                    }
                except ElementTree.ParseError:
                    result["xmp_warning"] = (
                        "Malformed XMP was retained without interpretation."
                    )
        result["raw"] = raw
        if len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
            raise ParserFailure("image_metadata_limit")
        result["gps_candidate"] = _gps(gps) if gps else None
        original_date = exif_fields.get(36867)
        date = original_date or exif.get(306)
        offset = exif_fields.get(36881) if original_date else exif_fields.get(36880)
        result["capture_time_raw"] = str(date) if date else None
        result["capture_offset_raw"] = str(offset) if offset else None
        if date:
            try:
                value = datetime.strptime(str(date), "%Y:%m:%d %H:%M:%S")
                if offset:
                    value = datetime.strptime(
                        str(date) + str(offset), "%Y:%m:%d %H:%M:%S%z"
                    )
                result["capture_time_candidate"] = value.isoformat()
            except ValueError:
                result["capture_time_candidate"] = None
        result["orientation"] = _safe_metadata(exif.get(274))
        return result


def _audio(content):
    try:
        with wave.open(io.BytesIO(content), "rb") as recording:
            channels = recording.getnchannels()
            width = recording.getsampwidth()
            rate = recording.getframerate()
            frames = recording.getnframes()
            if (
                recording.getcomptype() != "NONE"
                or width != 2
                or channels not in {1, 2}
            ):
                return {"status": "unsupported", "reason": "unsupported_audio_encoding"}
            if not 8000 <= rate <= 48000 or frames <= 0:
                return {"status": "unsupported", "reason": "unsupported_audio_rate"}
            duration = frames / rate
            if duration > MAX_AUDIO_SECONDS:
                raise ParserFailure("audio_duration_limit")
            expected = frames * width * channels
            if expected > MAX_BYTES:
                raise ParserFailure("input_limit")
            intervals = []
            offset = 0
            square_sum = 0
            sample_count = 0
            zero_samples = 0
            while offset < frames:
                count = min(rate, frames - offset)
                data = recording.readframes(count)
                if len(data) != count * width * channels:
                    raise ParserFailure("truncated_audio")
                samples = [value[0] for value in struct.iter_unpack("<h", data)]
                clipping = sum(abs(value) >= 32760 for value in samples) / len(samples)
                zeros = sum(value == 0 for value in samples)
                square_sum += sum(value * value for value in samples)
                sample_count += len(samples)
                zero_samples += zeros
                if zeros == len(samples) or clipping >= 0.01:
                    intervals.append(
                        {
                            "start": offset / rate,
                            "end": (offset + count) / rate,
                            "digital_zero": zeros == len(samples),
                            "full_scale_fraction": clipping,
                        }
                    )
                offset += count
            return {
                "status": "complete",
                "format": "WAV",
                "codec": "PCM signed 16-bit little-endian",
                "version": (
                    f"Python {sys.version_info.major}.{sys.version_info.minor} wave"
                ),
                "channels": channels,
                "sample_width_bytes": width,
                "sample_rate": rate,
                "frames": frames,
                "duration_seconds": duration,
                "rms_sample_amplitude": math.sqrt(square_sum / sample_count),
                "digital_zero": zero_samples == sample_count,
                "quality_intervals": intervals,
            }
    except wave.Error:
        return {"status": "unsupported", "reason": "unsupported_audio_format"}


def _audio_chunk(content, options):
    with wave.open(io.BytesIO(content), "rb") as recording:
        start, count = options.get("start_frame"), options.get("frame_count")
        rate = recording.getframerate()
        if (
            recording.getsampwidth() != 2
            or recording.getnchannels() not in {1, 2}
            or not 8000 <= rate <= 48000
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (start, count)
            )
            or not 0 <= start < recording.getnframes()
            or not 0 < count <= rate * 30
            or start + count > recording.getnframes()
        ):
            raise ParserFailure("invalid_audio_chunk")
        recording.setpos(start)
        data = recording.readframes(count)
        if len(data) != count * 2 * recording.getnchannels():
            raise ParserFailure("truncated_audio")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as chunk:
            chunk.setnchannels(recording.getnchannels())
            chunk.setsampwidth(2)
            chunk.setframerate(rate)
            chunk.writeframes(data)
        return {
            "audio": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "start_seconds": start / rate,
            "duration_seconds": count / rate,
            "digital_zero": not any(data),
        }


def _child():
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    logging.disable(logging.CRITICAL)
    content = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        if len(content) > MAX_BYTES:
            raise ParserFailure("input_limit")
        operation = sys.argv[1]
        if operation == "document":
            value = _document(content)
        elif operation == "image":
            value = _image(content)
        elif operation == "ocr_image":
            value = _image(content, prepare=True)
        elif operation == "audio":
            value = _audio(content)
        elif operation == "audio_chunk":
            value = _audio_chunk(content, json.loads(sys.argv[2]))
        elif operation == "geographic_context":
            # Load only this fixed sibling module; isolated mode supplies no
            # application/Django configuration to the geometry converter.
            convert = runpy.run_path(
                str(Path(__file__).with_name("geographic_geometry.py"))
            )["extract_context"]
            try:
                value = convert(content, json.loads(sys.argv[2]))
            except ValueError as error:
                if str(error).startswith("map_context_"):
                    raise ParserFailure(str(error)) from None
                raise
        else:
            raise ParserFailure("unsupported_parser")
        result = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(result) > MAX_OUTPUT:
            raise ParserFailure("parser_output_limit")
    except ParserFailure as error:
        result = json.dumps({"error": str(error)}).encode()
    except MemoryError:
        result = b'{"error":"parser_resource_limit"}'
    except Exception:
        result = b'{"error":"malformed_input"}'
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    _child()

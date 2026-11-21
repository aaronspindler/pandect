"""Pure image decoding, metadata and previews for the bounded child."""

import base64
import io
import json
import math
import warnings
from datetime import datetime
from xml.etree import ElementTree

from core.parser_contract import ParserFailure

MAX_PIXELS = 50_000_000
MAX_METADATA_BYTES = 256 * 1024


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


def _image(content, *, prepare=False, orient=False, max_side=2048):
    import PIL
    from PIL import ExifTags, Image, ImageOps

    if not (
        content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        return {"status": "unsupported", "reason": "unsupported_image_format"}
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(io.BytesIO(content), formats=["JPEG", "PNG"]) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ParserFailure("image_pixel_limit")
        image_count = getattr(image, "n_frames", 1)
        # JPEGs can contain auxiliary pictures (for example a phone gain map).
        # Decode only their primary picture; animated PNG remains unsupported.
        if image_count != 1 and image.format != "MPO":
            return {"status": "unsupported", "reason": "animated_image"}
        image.load()
        result = {
            "status": "complete",
            "version": f"Pillow {PIL.__version__}",
            "format": image.format,
            "image_count": image_count,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
        }
        if prepare:
            if orient:
                image = ImageOps.exif_transpose(image)
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", image.size, "white")
                prepared = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                prepared = image.convert("RGB")
            prepared.thumbnail((max_side, max_side))
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
            # Some camera exports NUL-terminate the XML packet. Keep the raw
            # source claim intact and ignore only terminal NULs for parsing.
            packet = xmp.rstrip("\x00")
            result["xmp_trailing_nuls"] = len(xmp) - len(packet)
            if "<!doctype" in xmp.lower() or "<!entity" in xmp.lower():
                result["xmp_warning"] = (
                    "DTD/entity-bearing XMP was retained without interpretation."
                )
            else:
                try:
                    tree = ElementTree.fromstring(packet)
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


def parse(operation, content, options):
    return _image(
        content,
        prepare=operation == "image_preview",
        orient=operation == "image_preview",
        max_side=320 if options.get("thumbnail") is True else 2048,
    )

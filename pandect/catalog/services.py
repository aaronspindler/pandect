"""Atomic construction and local media inspection for catalogue records."""

import json
import tempfile
from dataclasses import dataclass
from pathlib import PurePath
from struct import unpack

from django.conf import settings
from django.db import transaction

from pandect.audio.models import Audio
from pandect.catalog.models import CatalogItem
from pandect.events.models import Event
from pandect.messages.models import ImageMessage, Message, TextMessage, VideoMessage
from pandect.notes.models import Note
from pandect.photos.models import Image
from pandect.processing.extractors import ExtractorError
from pandect.processing.extractors.subprocesses import run_command
from pandect.videos.models import Video

_ITEM_TYPES = {
    Image: CatalogItem.ItemType.IMAGE,
    Video: CatalogItem.ItemType.VIDEO,
    Audio: CatalogItem.ItemType.AUDIO,
    Event: CatalogItem.ItemType.EVENT,
    Note: CatalogItem.ItemType.NOTE,
}

_MEDIA_MODELS = {
    CatalogItem.ItemType.IMAGE: Image,
    CatalogItem.ItemType.VIDEO: Video,
    CatalogItem.ItemType.AUDIO: Audio,
}

_MESSAGE_TYPES = {
    TextMessage: CatalogItem.ItemType.TEXT_MESSAGE,
    ImageMessage: CatalogItem.ItemType.IMAGE_MESSAGE,
    VideoMessage: CatalogItem.ItemType.VIDEO_MESSAGE,
}

_PROCESSABLE_ITEM_TYPES = {
    CatalogItem.ItemType.IMAGE,
    CatalogItem.ItemType.VIDEO,
    CatalogItem.ItemType.AUDIO,
    CatalogItem.ItemType.TEXT_MESSAGE,
    CatalogItem.ItemType.IMAGE_MESSAGE,
    CatalogItem.ItemType.VIDEO_MESSAGE,
}


def _create_automatic_processing_run(item: CatalogItem) -> None:
    """Create durable work in the intake transaction without importing eagerly."""
    if item.item_type not in _PROCESSABLE_ITEM_TYPES:
        return
    if (
        item.item_type
        in {
            CatalogItem.ItemType.TEXT_MESSAGE,
            CatalogItem.ItemType.IMAGE_MESSAGE,
            CatalogItem.ItemType.VIDEO_MESSAGE,
        }
        and not item.message.body.strip()
    ):
        return

    # Processing depends on the catalogue domain, so keep this import at the
    # explicit construction seam rather than creating a module import cycle.
    from pandect.processing.services import create_local_run

    create_local_run(item)


@dataclass(frozen=True)
class MediaInspection:
    """Facts read directly from an uploaded media file."""

    item_type: CatalogItem.ItemType
    mime_type: str
    width: int | None = None
    height: int | None = None


_MEDIA_PREFIX_BYTES = 512 * 1024
_FFPROBE_OUTPUT_BYTES = 16 * 1024 * 1024
_FFPROBE_TIMEOUT_SECONDS = 15
_AVIF_BRANDS = {b"avif", b"avis"}
_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx"}
_HEIF_BRANDS = {b"mif1", b"msf1"}


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Read JPEG frame dimensions without decoding or changing the image."""
    position = 2
    while position + 9 < len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if position + 2 > len(data):
            break
        segment_length = int.from_bytes(data[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            return width or None, height or None
        position += segment_length
    return None, None


def _webp_dimensions(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width or None, height or None
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        packed = int.from_bytes(data[21:25], "little")
        width = (packed & 0x3FFF) + 1
        height = ((packed >> 14) & 0x3FFF) + 1
        return width, height
    return None, None


def _iso_bmff_brands(data: bytes) -> set[bytes]:
    """Read the major and compatible brands from a bounded ftyp box."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return set()
    box_size = int.from_bytes(data[:4], "big")
    if box_size < 16:
        return set()
    box_end = min(box_size, len(data))
    return {data[8:12]} | {
        data[position : position + 4] for position in range(16, box_end - 3, 4)
    }


def _run_ffprobe(upload) -> dict | None:
    """Return a small ffprobe stream summary, or None when probing is unavailable."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-probesize",
        str(_MEDIA_PREFIX_BYTES),
        "-analyzeduration",
        "1000000",
        "-show_entries",
        (
            "format=format_name:stream=index,codec_type,width,height:"
            "stream_disposition=attached_pic"
        ),
        "-of",
        "json",
    ]
    temporary_probe = None
    try:
        temporary_path = upload.temporary_file_path()
    except (AttributeError, OSError):
        original_position = upload.tell()
        try:
            upload.seek(0)
            temporary_probe = tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix="intake-probe-",
                suffix=".media",
                dir=settings.FILE_UPLOAD_TEMP_DIR,
            )
            while chunk := upload.read(64 * 1024):
                temporary_probe.write(chunk)
            temporary_probe.flush()
            temporary_path = temporary_probe.name
        finally:
            upload.seek(original_position)
    command.append(str(temporary_path))

    try:
        completed = run_command(
            command,
            timeout_seconds=_FFPROBE_TIMEOUT_SECONDS,
            output_limit_bytes=_FFPROBE_OUTPUT_BYTES,
        )
    except ExtractorError:
        return None
    finally:
        if temporary_probe is not None:
            temporary_probe.close()
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _inspection_from_probe(
    payload: dict,
    *,
    container: str,
    image_mime_type: str | None = None,
) -> MediaInspection | None:
    """Classify an ambiguous container from a validated ffprobe summary."""
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict)
        and stream.get("codec_type") == "video"
        and not (
            isinstance(stream.get("disposition"), dict)
            and stream["disposition"].get("attached_pic")
        )
    ]

    if video_streams:
        stream = video_streams[0]
        if image_mime_type is not None and not audio_streams:
            width = stream.get("width")
            height = stream.get("height")
            return MediaInspection(
                CatalogItem.ItemType.IMAGE,
                image_mime_type,
                width if isinstance(width, int) and width > 0 else None,
                height if isinstance(height, int) and height > 0 else None,
            )
        mime_type = {
            "quicktime": "video/quicktime",
            "iso-bmff": "video/mp4",
            "webm": "video/webm",
            "matroska": "video/x-matroska",
        }[container]
        width = stream.get("width")
        height = stream.get("height")
        return MediaInspection(
            CatalogItem.ItemType.VIDEO,
            mime_type,
            width if isinstance(width, int) and width > 0 else None,
            height if isinstance(height, int) and height > 0 else None,
        )
    if audio_streams:
        mime_type = {
            "quicktime": "audio/mp4",
            "iso-bmff": "audio/mp4",
            "webm": "audio/webm",
            "matroska": "audio/x-matroska",
        }[container]
        return MediaInspection(CatalogItem.ItemType.AUDIO, mime_type)
    return None


def _probe_ambiguous_container(
    upload,
    *,
    container: str,
    image_mime_type: str | None = None,
) -> MediaInspection | None:
    payload = _run_ffprobe(upload)
    if payload is None:
        return None
    return _inspection_from_probe(
        payload,
        container=container,
        image_mime_type=image_mime_type,
    )


def inspect_uploaded_media(upload) -> MediaInspection:
    """Detect supported media from file bytes and extract safe local facts."""
    original_position = upload.tell()
    try:
        upload.seek(0)
        data = upload.read(_MEDIA_PREFIX_BYTES)
    finally:
        upload.seek(original_position)

    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = unpack(">II", data[16:24])
        return MediaInspection(CatalogItem.ItemType.IMAGE, "image/png", width, height)
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        width, height = unpack("<HH", data[6:10])
        return MediaInspection(CatalogItem.ItemType.IMAGE, "image/gif", width, height)
    if data.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_dimensions(data)
        return MediaInspection(CatalogItem.ItemType.IMAGE, "image/jpeg", width, height)
    if data.startswith(b"BM") and len(data) >= 26:
        width = abs(int.from_bytes(data[18:22], "little", signed=True)) or None
        height = abs(int.from_bytes(data[22:26], "little", signed=True)) or None
        return MediaInspection(CatalogItem.ItemType.IMAGE, "image/bmp", width, height)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        width, height = _webp_dimensions(data)
        return MediaInspection(CatalogItem.ItemType.IMAGE, "image/webp", width, height)
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return MediaInspection(CatalogItem.ItemType.IMAGE, "image/tiff")

    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/wav")
    if data.startswith(b"fLaC"):
        return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/flac")
    if data.startswith(b"ID3"):
        return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/mpeg")
    if data.startswith(b"FORM") and data[8:12] in {b"AIFF", b"AIFC"}:
        return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/aiff")
    if data.startswith(b"caff"):
        return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/x-caf")
    if data.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/amr")
    if data.startswith(b"OggS"):
        codec_prefix = data[:4096]
        if any(
            marker in codec_prefix
            for marker in (b"OpusHead", b"vorbis", b"Speex", b"fLaC")
        ):
            return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/ogg")
        if b"theora" in codec_prefix:
            return MediaInspection(CatalogItem.ItemType.VIDEO, "video/ogg")
    if len(data) >= 2 and data[0] == 0xFF:
        if data[1] & 0xF6 == 0xF0:
            return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/aac")
        if data[1] & 0xE0 == 0xE0 and data[1] & 0x06:
            return MediaInspection(CatalogItem.ItemType.AUDIO, "audio/mpeg")

    if len(data) >= 16 and data[4:8] == b"ftyp":
        brands = _iso_bmff_brands(data)
        image_mime_type = None
        if brands & _AVIF_BRANDS:
            image_mime_type = "image/avif"
        elif brands & _HEIC_BRANDS:
            image_mime_type = "image/heic"
        elif brands & _HEIF_BRANDS:
            image_mime_type = "image/heif"

        container = "quicktime" if b"qt  " in brands else "iso-bmff"
        probed = _probe_ambiguous_container(
            upload,
            container=container,
            image_mime_type=image_mime_type,
        )
        if probed is not None:
            return probed
        raise ValueError(
            "This ISO-BMFF file needs the local ffprobe tool to distinguish "
            "image, audio, and video streams."
        )
    if data.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return MediaInspection(CatalogItem.ItemType.VIDEO, "video/x-msvideo")
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        header = data[:4096].lower()
        container = "webm" if b"webm" in header else "matroska"
        probed = _probe_ambiguous_container(upload, container=container)
        if probed is not None:
            return probed
        raise ValueError(
            "This WebM/Matroska file needs the local ffprobe tool to distinguish "
            "audio from video."
        )
    if data.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return MediaInspection(CatalogItem.ItemType.VIDEO, "video/mpeg")

    raise ValueError(
        "The file is not a supported image, video, or audio file, or its type could "
        "not be verified from its contents."
    )


def _original_filename(upload) -> str:
    """Keep a storage-neutral basename within the model's metadata limit."""
    filename = str(upload.name).replace("\\", "/").rsplit("/", 1)[-1]
    if len(filename) <= 255:
        return filename
    suffix = PurePath(filename).suffix
    return f"{PurePath(filename).stem[: 255 - len(suffix)]}{suffix}"


def _title_from_filename(filename: str) -> str:
    return PurePath(filename).stem[:255]


def create_uploaded_media(upload, *, expected_type=None, inspection=None):
    """Inspect and atomically catalogue one original local media file."""
    inspection = inspection or inspect_uploaded_media(upload)
    if expected_type is not None and inspection.item_type != expected_type:
        expected_label = CatalogItem.ItemType(expected_type).label.lower()
        raise ValueError(f"Select a {expected_label} file for this item type.")

    filename = _original_filename(upload)
    model = _MEDIA_MODELS[inspection.item_type]
    fields = {
        "file": upload,
        "mime_type": inspection.mime_type,
        "original_filename": filename,
    }
    if inspection.item_type in {
        CatalogItem.ItemType.IMAGE,
        CatalogItem.ItemType.VIDEO,
    }:
        fields |= {"width": inspection.width, "height": inspection.height}
    saved_file = None
    try:
        with transaction.atomic():
            extension = create_catalogued_item(
                model,
                item_fields={"title": _title_from_filename(filename)},
                extension_fields=fields,
            )
            saved_file = (extension.file.storage, extension.file.name)
            _create_automatic_processing_run(extension.catalog_item)
            return extension
    except Exception:
        if saved_file:
            saved_file[0].delete(saved_file[1])
        raise


def create_uploaded_media_batch(uploads, *, inspections=None):
    """Validate then atomically catalogue a batch of original media files."""
    inspections = inspections or [inspect_uploaded_media(upload) for upload in uploads]
    if len(uploads) != len(inspections):
        raise ValueError("Each upload requires one media inspection.")
    saved_files = []
    created_items = []
    try:
        with transaction.atomic():
            for upload, inspection in zip(uploads, inspections, strict=True):
                filename = _original_filename(upload)
                model = _MEDIA_MODELS[inspection.item_type]
                fields = {
                    "file": upload,
                    "mime_type": inspection.mime_type,
                    "original_filename": filename,
                }
                if inspection.item_type in {
                    CatalogItem.ItemType.IMAGE,
                    CatalogItem.ItemType.VIDEO,
                }:
                    fields |= {
                        "width": inspection.width,
                        "height": inspection.height,
                    }
                extension = create_catalogued_item(
                    model,
                    item_fields={"title": _title_from_filename(filename)},
                    extension_fields=fields,
                )
                saved_files.append((extension.file.storage, extension.file.name))
                _create_automatic_processing_run(extension.catalog_item)
                created_items.append(extension.catalog_item)
    except Exception:
        for storage, name in saved_files:
            storage.delete(name)
        raise
    return created_items


@transaction.atomic
def create_catalogued_item(
    model,
    *,
    item_fields=None,
    extension_fields=None,
):
    """Create a non-message catalogue item and its extension atomically."""
    try:
        item_type = _ITEM_TYPES[model]
    except KeyError as error:
        raise ValueError("Unsupported catalogue item model.") from error

    item = CatalogItem.objects.create(item_type=item_type, **(item_fields or {}))
    return model.objects.create(
        catalog_item=item,
        **(extension_fields or {}),
    )


@transaction.atomic
def create_catalogued_message(
    model,
    *,
    item_fields=None,
    message_fields=None,
    subtype_fields=None,
):
    """Create a catalogue item, message, and concrete subtype atomically."""
    try:
        item_type = _MESSAGE_TYPES[model]
    except KeyError as error:
        raise ValueError("Unsupported message model.") from error

    item = CatalogItem.objects.create(item_type=item_type, **(item_fields or {}))
    message = Message.objects.create(
        catalog_item=item,
        **(message_fields or {}),
    )
    subtype = model.objects.create(message=message, **(subtype_fields or {}))
    _create_automatic_processing_run(item)
    return subtype

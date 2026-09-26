"""Atomic construction and local media inspection for catalogue records."""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from struct import unpack
from uuid import uuid4

from django.conf import settings
from django.db import transaction

from pandect.audio.models import Audio
from pandect.catalog.models import CatalogItem
from pandect.catalog.uploads import UploadLimitError, validate_upload_sizes
from pandect.core.storage import StorageCapacityError, require_storage_space
from pandect.events.models import Event
from pandect.messages.models import ImageMessage, Message, TextMessage, VideoMessage
from pandect.notes.models import Note
from pandect.photos.models import Image
from pandect.processing.extractors import ExtractorError
from pandect.processing.extractors.ffprobe import probe_media
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
    try:
        metadata = probe_media(
            temporary_path,
            timeout_seconds=_FFPROBE_TIMEOUT_SECONDS,
        )
    except ExtractorError:
        return None
    finally:
        if temporary_probe is not None:
            temporary_probe.close()
    return metadata.raw


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


def _store_original_upload(upload, model, *, remaining_batch_bytes):
    """Publish one exclusive original, cleaning only our provisional file.

    Django's normal FileField pre-save can leave a file behind when the later
    model insert fails, or when storage.save itself raises after opening it.
    Own the temporary path and final publication here so both cases roll back.
    """
    field = model._meta.get_field("file")
    suffix = PurePath(_original_filename(upload)).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        suffix = ""
    name = field.generate_filename(model(), f"{uuid4().hex}{suffix}")
    target = Path(field.storage.path(name))
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
    written = 0
    published = False
    completed = False
    position = upload.tell()
    try:
        require_storage_space(target.parent, upload.size)
        new_directories = []
        ancestor = target.parent
        while not ancestor.exists():
            new_directories.append(ancestor)
            ancestor = ancestor.parent
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            upload.seek(0)
            while chunk := upload.read(64 * 1024):
                written += len(chunk)
                if written > settings.UPLOAD_MAX_FILE_BYTES:
                    raise UploadLimitError("The file exceeds the upload size limit.")
                if written > remaining_batch_bytes:
                    raise UploadLimitError("The batch exceeds the upload size limit.")
                require_storage_space(target.parent, len(chunk))
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        # link() never replaces an existing original, even on a name collision.
        require_storage_space(target.parent)
        os.link(temporary, target)
        published = True
        temporary.unlink()
        # File fsync alone does not persist its new directory entry. Include
        # newly-created ancestor entries so first intake is durable too, before
        # the caller can commit a catalogue row pointing at this original.
        directories = [target.parent, *(path.parent for path in new_directories)]
        for directory in dict.fromkeys(directories):
            directory_fd = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        completed = True
        return field.storage, name, written
    except OSError as error:
        raise StorageCapacityError(
            "The original could not be stored. Check the data volume's "
            "free space and permissions, then retry."
        ) from error
    finally:
        upload.seek(position)
        # Once returned, the caller owns this exact exclusive path. A failed
        # publication must not remove a pre-existing collision target.
        if published and not completed:
            target.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)


def create_uploaded_media(upload, *, expected_type=None, inspection=None):
    """Inspect and atomically catalogue one original local media file."""
    validate_upload_sizes([upload])
    inspection = inspection or inspect_uploaded_media(upload)
    if expected_type is not None and inspection.item_type != expected_type:
        expected_label = CatalogItem.ItemType(expected_type).label.lower()
        raise ValueError(f"Select a {expected_label} file for this item type.")

    filename = _original_filename(upload)
    model = _MEDIA_MODELS[inspection.item_type]
    fields = {
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
            storage, name, _size = _store_original_upload(
                upload, model, remaining_batch_bytes=settings.UPLOAD_MAX_BATCH_BYTES
            )
            saved_file = (storage, name)
            extension = create_catalogued_item(
                model,
                item_fields={"title": _title_from_filename(filename)},
                extension_fields=fields | {"file": name},
            )
            _create_automatic_processing_run(extension.catalog_item)
            return extension
    except Exception:
        if saved_file:
            saved_file[0].delete(saved_file[1])
        raise


def create_uploaded_media_batch(uploads, *, inspections=None):
    """Validate then atomically catalogue a batch of original media files."""
    uploads = list(uploads)
    validate_upload_sizes(uploads)
    inspections = inspections or [inspect_uploaded_media(upload) for upload in uploads]
    if len(uploads) != len(inspections):
        raise ValueError("Each upload requires one media inspection.")
    saved_files = []
    created_items = []
    stored_bytes = 0
    try:
        with transaction.atomic():
            for upload, inspection in zip(uploads, inspections, strict=True):
                filename = _original_filename(upload)
                model = _MEDIA_MODELS[inspection.item_type]
                fields = {
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
                storage, name, size = _store_original_upload(
                    upload,
                    model,
                    remaining_batch_bytes=settings.UPLOAD_MAX_BATCH_BYTES
                    - stored_bytes,
                )
                saved_files.append((storage, name))
                stored_bytes += size
                extension = create_catalogued_item(
                    model,
                    item_fields={"title": _title_from_filename(filename)},
                    extension_fields=fields | {"file": name},
                )
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

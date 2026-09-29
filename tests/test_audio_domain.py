import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from pandect.audio.models import Audio
from pandect.catalog import services
from pandect.catalog.models import CatalogItem
from pandect.catalog.services import inspect_uploaded_media
from pandect.processing.extractors import ToolOutputLimitError, ToolUnavailableError
from pandect.processing.extractors.ffprobe import parse_ffprobe_json

pytestmark = pytest.mark.django_db


def upload(name, contents, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, contents, content_type=content_type)


def iso_bmff(major_brand, *compatible_brands):
    size = 16 + 4 * len(compatible_brands)
    return (
        size.to_bytes(4, "big")
        + b"ftyp"
        + major_brand
        + b"\x00\x00\x00\x00"
        + b"".join(compatible_brands)
    )


def wav_bytes():
    return b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 16


def test_audio_model_is_a_rooted_catalogue_extension():
    item = CatalogItem.objects.create(
        item_type=CatalogItem.ItemType.AUDIO,
        title="Interview",
    )
    audio = Audio.objects.create(
        catalog_item=item,
        file="catalog/audio/interview.wav",
        mime_type="audio/wav",
        original_filename="interview.wav",
        duration=timedelta(minutes=3),
    )

    assert item.audio == audio
    with pytest.raises(TypeError, match="through CatalogItem"):
        audio.delete()


def test_audio_rejects_negative_duration_and_mismatched_root_type():
    with pytest.raises(ValidationError):
        Audio.objects.create(
            catalog_item=CatalogItem.objects.create(
                item_type=CatalogItem.ItemType.AUDIO
            ),
            duration=timedelta(seconds=-1),
        )
    with pytest.raises(ValidationError, match="Catalog item type"):
        Audio.objects.create(
            catalog_item=CatalogItem.objects.create(
                item_type=CatalogItem.ItemType.VIDEO
            )
        )


@pytest.mark.parametrize(
    ("name", "contents", "mime_type"),
    [
        ("recording.wav", wav_bytes(), "audio/wav"),
        ("recording.flac", b"fLaC" + b"\x00" * 32, "audio/flac"),
        ("recording.mp3", b"ID3\x04\x00\x00" + b"\x00" * 32, "audio/mpeg"),
        ("recording.aiff", b"FORM\x00\x00\x00\x10AIFF" + b"\x00" * 16, "audio/aiff"),
        ("recording.aac", b"\xff\xf1" + b"\x00" * 32, "audio/aac"),
    ],
)
def test_common_audio_signatures_are_classified_without_browser_mime(
    name, contents, mime_type
):
    inspection = inspect_uploaded_media(upload(name, contents, "text/plain"))

    assert inspection.item_type == CatalogItem.ItemType.AUDIO
    assert inspection.mime_type == mime_type


@pytest.mark.parametrize(
    ("major_brand", "compatible_brand", "mime_type"),
    [
        (b"avif", b"mif1", "image/avif"),
        (b"heic", b"mif1", "image/heic"),
        (b"mif1", b"msf1", "image/heif"),
    ],
)
def test_iso_bmff_image_brands_are_not_misclassified_as_video(
    monkeypatch, major_brand, compatible_brand, mime_type
):
    monkeypatch.setattr(
        services,
        "_run_ffprobe",
        lambda uploaded: {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 640,
                    "height": 480,
                }
            ]
        },
    )

    inspection = inspect_uploaded_media(
        upload("image.bin", iso_bmff(major_brand, compatible_brand))
    )

    assert inspection.item_type == CatalogItem.ItemType.IMAGE
    assert inspection.mime_type == mime_type
    assert (inspection.width, inspection.height) == (640, 480)


def test_m4a_brand_is_not_classified_without_ffprobe_stream_truth(monkeypatch):
    monkeypatch.setattr(services, "_run_ffprobe", lambda uploaded: None)

    with pytest.raises(ValueError, match="needs the local ffprobe tool"):
        inspect_uploaded_media(upload("recording.m4a", iso_bmff(b"M4A ", b"isom")))


def test_iso_bmff_image_brand_is_not_classified_without_stream_truth(monkeypatch):
    monkeypatch.setattr(services, "_run_ffprobe", lambda uploaded: None)

    with pytest.raises(ValueError, match="needs the local ffprobe tool"):
        inspect_uploaded_media(upload("image.avif", iso_bmff(b"avif", b"mif1")))


@pytest.mark.parametrize(
    ("streams", "expected_type", "mime_type", "dimensions"),
    [
        (
            [{"codec_type": "audio"}],
            CatalogItem.ItemType.AUDIO,
            "audio/mp4",
            (None, None),
        ),
        (
            [{"codec_type": "video", "width": 1920, "height": 1080}],
            CatalogItem.ItemType.VIDEO,
            "video/mp4",
            (1920, 1080),
        ),
        (
            [
                {"codec_type": "audio"},
                {"codec_type": "video", "disposition": {"attached_pic": 1}},
            ],
            CatalogItem.ItemType.AUDIO,
            "audio/mp4",
            (None, None),
        ),
    ],
)
def test_ffprobe_streams_distinguish_audio_video_and_cover_art(
    monkeypatch, streams, expected_type, mime_type, dimensions
):
    monkeypatch.setattr(services, "_run_ffprobe", lambda uploaded: {"streams": streams})

    inspection = inspect_uploaded_media(
        upload("ambiguous.mp4", iso_bmff(b"isom", b"mp42"))
    )

    assert inspection.item_type == expected_type
    assert inspection.mime_type == mime_type
    assert (inspection.width, inspection.height) == dimensions


@pytest.mark.parametrize(
    ("doc_type", "streams", "expected_type", "mime_type"),
    [
        (b"webm", [{"codec_type": "audio"}], CatalogItem.ItemType.AUDIO, "audio/webm"),
        (
            b"webm",
            [{"codec_type": "video", "width": 640, "height": 360}],
            CatalogItem.ItemType.VIDEO,
            "video/webm",
        ),
        (
            b"matroska",
            [{"codec_type": "audio"}],
            CatalogItem.ItemType.AUDIO,
            "audio/x-matroska",
        ),
    ],
)
def test_ffprobe_classifies_ebml_audio_and_video(
    monkeypatch, doc_type, streams, expected_type, mime_type
):
    monkeypatch.setattr(services, "_run_ffprobe", lambda uploaded: {"streams": streams})
    contents = b"\x1a\x45\xdf\xa3" + b"\x00" * 8 + doc_type

    inspection = inspect_uploaded_media(upload("evidence.mkv", contents))

    assert inspection.item_type == expected_type
    assert inspection.mime_type == mime_type


def test_ambiguous_ebml_reports_missing_probe_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(services, "_run_ffprobe", lambda uploaded: None)

    with pytest.raises(ValueError, match="needs the local ffprobe tool"):
        inspect_uploaded_media(upload("evidence.webm", b"\x1a\x45\xdf\xa3\x00webm"))


def test_ffprobe_unavailable_is_bounded_and_restores_upload_position(monkeypatch):
    media = upload("evidence.mp4", iso_bmff(b"isom", b"mp42"))
    media.seek(3)
    observed_path = None

    def unavailable(path, **kwargs):
        nonlocal observed_path
        observed_path = Path(path)
        assert observed_path.parent == Path(settings.FILE_UPLOAD_TEMP_DIR)
        assert observed_path.is_file()
        assert kwargs["timeout_seconds"] == 15
        raise ToolUnavailableError

    monkeypatch.setattr(services, "probe_media", unavailable)

    assert services._run_ffprobe(media) is None
    assert media.tell() == 3
    assert observed_path is not None
    assert not observed_path.exists()


def test_ffprobe_rejects_unbounded_or_invalid_output(monkeypatch):
    media = upload("evidence.mp4", iso_bmff(b"isom", b"mp42"))

    def output_too_large(*args, **kwargs):
        raise ToolOutputLimitError

    monkeypatch.setattr(services, "probe_media", output_too_large)
    assert services._run_ffprobe(media) is None

    monkeypatch.setattr(
        services,
        "probe_media",
        lambda *args, **kwargs: parse_ffprobe_json(json.dumps(["not", "an", "object"])),
    )
    assert services._run_ffprobe(media) is None


def test_single_audio_intake_preserves_original_and_reviews_metadata(client):
    response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "audio"}),
        {"file": upload("interview.wav", wav_bytes(), "text/plain")},
    )

    assert response.status_code == 302
    item = CatalogItem.objects.get()
    audio = item.audio
    assert item.item_type == CatalogItem.ItemType.AUDIO
    assert item.title == "interview"
    assert audio.original_filename == "interview.wav"
    assert audio.mime_type == "audio/wav"
    with audio.file.open("rb") as stored_file:
        assert stored_file.read() == wav_bytes()

    review = client.get(response["Location"])
    assert review.context["form"]["original_filename"].value() == "interview.wav"
    assert review.context["form"]["mime_type"].value() == "audio/wav"
    assert "duration" in review.context["form"].fields


def test_audio_appears_in_bulk_intake_and_catalogue_summary(client):
    response = client.post(
        reverse("catalogue-bulk-add"),
        {"files": [upload("recording.wav", wav_bytes())]},
    )

    assert response.status_code == 302
    item = CatalogItem.objects.get()
    item.audio.duration = timedelta(seconds=5)
    item.audio.save()
    catalogue = client.get(reverse("catalogue"), {"type": "audio"})
    content = catalogue.content.decode()
    assert "recording.wav" in content
    assert "audio/wav · 0:00:05" in content


def test_audio_does_not_create_an_audio_message_type(client):
    response = client.get(
        reverse("catalogue-add-item", kwargs={"item_type": "audio_message"})
    )

    assert response.status_code == 404
    assert "audio_message" not in dict(CatalogItem.ItemType.choices)

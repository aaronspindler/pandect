"""Upload byte accounting, disk guardrails, and provisional-original rollback."""

import errno
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import StopUpload
from django.urls import reverse

from pandect.catalog.models import CatalogItem
from pandect.catalog.services import (
    MediaInspection,
    create_uploaded_media,
    create_uploaded_media_batch,
)
from pandect.catalog.uploads import GuardedUploadHandler, UploadLimitError
from pandect.core.storage import StorageCapacityError, require_storage_space
from pandect.photos.models import Image

pytestmark = pytest.mark.django_db

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + (16).to_bytes(4, "big")
    + (9).to_bytes(4, "big")
    + b"\x08\x06\x00\x00\x00"
)
INSPECTION = MediaInspection(CatalogItem.ItemType.IMAGE, "image/png", 16, 9)


@pytest.fixture(autouse=True)
def upload_settings(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.FILE_UPLOAD_TEMP_DIR = tmp_path / "uploads"
    settings.FILE_UPLOAD_TEMP_DIR.mkdir()
    settings.UPLOAD_MAX_FILE_BYTES = 256 * 1024
    settings.UPLOAD_MAX_BATCH_BYTES = 512 * 1024
    settings.UPLOAD_MAX_FILES = 3
    settings.STORAGE_FREE_RESERVE_BYTES = 1024
    settings.FILE_UPLOAD_HANDLERS = ["pandect.catalog.uploads.GuardedUploadHandler"]
    middleware = "pandect.catalog.uploads.UploadRejectionMiddleware"
    settings.MIDDLEWARE = [middleware] + [
        item for item in settings.MIDDLEWARE if item != middleware
    ]


def png_upload(name="synthetic.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


def stored_files(settings):
    return [path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()]


def handler_with_file(content_length=None):
    request = SimpleNamespace()
    handler = GuardedUploadHandler(request)
    handler.new_file(
        "files", "synthetic.bin", "application/octet-stream", content_length
    )
    return handler, request


@pytest.mark.parametrize("declared_length", [None, 0, 1, 10**12])
def test_handler_counts_actual_bytes_not_file_content_length(settings, declared_length):
    settings.UPLOAD_MAX_FILE_BYTES = 4
    handler, request = handler_with_file(declared_length)
    try:
        handler.receive_data_chunk(b"1234", 0)
        assert handler.file.tell() == 4
        with pytest.raises(StopUpload):
            handler.receive_data_chunk(b"5", 4)
        assert handler.file.tell() == 4
        assert request.upload_rejection[1] == 413
    finally:
        handler.file.close()


def test_handler_counts_batch_bytes_across_files(settings):
    settings.UPLOAD_MAX_BATCH_BYTES = 5
    handler, request = handler_with_file()
    first = None
    try:
        handler.receive_data_chunk(b"123", 0)
        first = handler.file_complete(3)
        handler.new_file("files", "second.bin", "application/octet-stream", None)
        handler.receive_data_chunk(b"45", 0)
        with pytest.raises(StopUpload):
            handler.receive_data_chunk(b"6", 2)
        assert handler.file.tell() == 2
        assert "batch" in request.upload_rejection[0]
    finally:
        if first is not None:
            first.close()
        handler.file.close()


def test_handler_storage_write_failure_has_safe_rejection(monkeypatch):
    handler, request = handler_with_file()
    try:
        monkeypatch.setattr(
            handler.file.file,
            "write",
            Mock(side_effect=OSError(errno.ENOSPC, "/private/evidence/secret-name")),
        )
        with pytest.raises(StopUpload):
            handler.receive_data_chunk(b"123", 0)
        message, status = request.upload_rejection
        assert status == 507
        assert "secret-name" not in message
    finally:
        handler.file.close()


def test_client_rejects_one_byte_over_and_removes_all_temporary_files(client, settings):
    settings.UPLOAD_MAX_FILE_BYTES = len(PNG) - 1
    response = client.post(reverse("catalogue-bulk-add"), {"files": [png_upload()]})
    assert response.status_code == 413
    assert not CatalogItem.objects.exists()
    assert not list(settings.FILE_UPLOAD_TEMP_DIR.iterdir())
    assert not stored_files(settings)


def test_client_accepts_exact_file_and_batch_limits(client, settings):
    settings.UPLOAD_MAX_FILE_BYTES = len(PNG)
    settings.UPLOAD_MAX_BATCH_BYTES = 2 * len(PNG)
    response = client.post(
        reverse("catalogue-bulk-add"), {"files": [png_upload("one.png"), png_upload()]}
    )
    assert response.status_code == 302
    assert CatalogItem.objects.count() == 2
    assert len(stored_files(settings)) == 2


def test_client_rejects_entire_batch_on_byte_overflow(client, settings):
    settings.UPLOAD_MAX_BATCH_BYTES = 2 * len(PNG) - 1
    response = client.post(
        reverse("catalogue-bulk-add"), {"files": [png_upload("one.png"), png_upload()]}
    )
    assert response.status_code == 413
    assert not CatalogItem.objects.exists()
    assert not list(settings.FILE_UPLOAD_TEMP_DIR.iterdir())


def test_client_rejects_entire_batch_on_count_overflow(client, settings):
    response = client.post(
        reverse("catalogue-bulk-add"),
        {"files": [png_upload(f"{number}.png") for number in range(4)]},
    )
    assert response.status_code == 413
    assert b"no more than 3" in response.content
    assert not CatalogItem.objects.exists()
    assert not list(settings.FILE_UPLOAD_TEMP_DIR.iterdir())


def test_low_disk_rejects_before_temporary_file_creation(client, settings, monkeypatch):
    monkeypatch.setattr(
        "pandect.core.storage.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1023),
    )
    response = client.post(reverse("catalogue-bulk-add"), {"files": [png_upload()]})
    assert response.status_code == 507
    assert b"Free space" in response.content
    assert not CatalogItem.objects.exists()
    assert not list(settings.FILE_UPLOAD_TEMP_DIR.iterdir())


def test_storage_reserve_is_required_in_addition_to_next_write(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pandect.core.storage.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1028),
    )
    require_storage_space(tmp_path / "not-created", 4)
    with pytest.raises(StorageCapacityError):
        require_storage_space(tmp_path / "not-created", 5)
    assert not (tmp_path / "not-created").exists()


def test_original_copy_rechecks_space_before_creating_directory(settings, monkeypatch):
    monkeypatch.setattr(
        "pandect.core.storage.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1024),
    )
    with pytest.raises(StorageCapacityError):
        create_uploaded_media(png_upload(), inspection=INSPECTION)
    assert not CatalogItem.objects.exists()
    assert not Path(settings.MEDIA_ROOT).exists()


def test_database_failure_removes_published_provisional_original(settings, monkeypatch):
    monkeypatch.setattr(
        "pandect.catalog.services.create_catalogued_item",
        Mock(side_effect=RuntimeError("synthetic database insert failure")),
    )
    with pytest.raises(RuntimeError):
        create_uploaded_media(png_upload(), inspection=INSPECTION)
    assert not CatalogItem.objects.exists()
    assert not stored_files(settings)


def test_original_and_new_directory_entries_are_synced_before_database_insert(
    settings, monkeypatch
):
    import pandect.catalog.services as services

    real_fsync = os.fsync
    real_create = services.create_catalogued_item
    synced_directories = set()
    write_order = []

    def record_sync(descriptor):
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            synced_directories.add((info.st_dev, info.st_ino))
            write_order.append("directory")
        else:
            write_order.append("file")
        return real_fsync(descriptor)

    def assert_sync_before_insert(*args, **kwargs):
        directory = Path(settings.MEDIA_ROOT) / "catalog/images"
        expected = {directory, directory.parent, directory.parent.parent}
        expected.add(Path(settings.MEDIA_ROOT).parent)
        assert synced_directories == {
            (path.stat().st_dev, path.stat().st_ino) for path in expected
        }
        assert write_order == [
            "file",
            "directory",
            "directory",
            "directory",
            "directory",
        ]
        return real_create(*args, **kwargs)

    monkeypatch.setattr(services.os, "fsync", record_sync)
    monkeypatch.setattr(services, "create_catalogued_item", assert_sync_before_insert)
    image = create_uploaded_media(png_upload(), inspection=INSPECTION)
    assert Path(image.file.path).read_bytes() == PNG


@pytest.mark.parametrize("failed_directory_sync", [1, 2])
def test_directory_sync_failure_rolls_back_original_before_database_insert(
    settings, monkeypatch, failed_directory_sync
):
    import pandect.catalog.services as services

    real_fsync = os.fsync
    directory_syncs = 0

    def fail_directory_sync(descriptor):
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
            if directory_syncs == failed_directory_sync:
                raise OSError(errno.EIO, "synthetic directory fsync failure")
        return real_fsync(descriptor)

    create = Mock()
    monkeypatch.setattr(services.os, "fsync", fail_directory_sync)
    monkeypatch.setattr(services, "create_catalogued_item", create)
    with pytest.raises(StorageCapacityError, match="could not be stored"):
        create_uploaded_media(png_upload(), inspection=INSPECTION)
    create.assert_not_called()
    assert not CatalogItem.objects.exists()
    assert not stored_files(settings)


def test_batch_directory_sync_failure_preserves_existing_evidence(
    settings, monkeypatch
):
    import pandect.catalog.services as services

    existing = create_uploaded_media(png_upload("existing.png"), inspection=INSPECTION)
    original_path = Path(existing.file.path)
    real_fsync = os.fsync
    directory_syncs = 0

    def fail_second_original_directory_sync(descriptor):
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
            if directory_syncs == 2:
                raise OSError(errno.EIO, "synthetic directory fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(services.os, "fsync", fail_second_original_directory_sync)
    with pytest.raises(StorageCapacityError):
        create_uploaded_media_batch(
            [png_upload("first.png"), png_upload("second.png")],
            inspections=[INSPECTION, INSPECTION],
        )
    assert directory_syncs == 2
    assert list(CatalogItem.objects.values_list("pk", flat=True)) == [existing.pk]
    assert stored_files(settings) == [original_path]
    assert original_path.read_bytes() == PNG


def test_batch_failure_rolls_back_rows_and_new_files_not_existing_evidence(
    settings, monkeypatch
):
    import pandect.catalog.services as services

    existing = create_uploaded_media(png_upload("existing.png"), inspection=INSPECTION)
    existing_path = Path(existing.file.path)
    original_create = services.create_catalogued_item
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second insert failure")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(services, "create_catalogued_item", fail_second)
    with pytest.raises(RuntimeError):
        create_uploaded_media_batch(
            [png_upload("first.png"), png_upload("second.png")],
            inspections=[INSPECTION, INSPECTION],
        )
    assert list(CatalogItem.objects.values_list("pk", flat=True)) == [existing.pk]
    assert stored_files(settings) == [existing_path]
    assert existing_path.read_bytes() == PNG


def test_interrupted_original_write_removes_its_partial_file(settings, monkeypatch):
    upload = SimpleUploadedFile("large.png", b"x" * 128 * 1024)
    real_read = upload.read
    calls = 0

    def fail_after_first_chunk(size):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "private path must not be displayed")
        return real_read(size)

    monkeypatch.setattr(upload.file, "read", fail_after_first_chunk)
    with pytest.raises(StorageCapacityError, match="could not be stored"):
        create_uploaded_media(upload, inspection=INSPECTION)
    assert not CatalogItem.objects.exists()
    assert not stored_files(settings)


def test_service_copy_does_not_trust_upload_size(settings):
    settings.UPLOAD_MAX_FILE_BYTES = len(PNG) - 1
    upload = png_upload()
    upload.size = 1
    with pytest.raises(UploadLimitError):
        create_uploaded_media(upload, inspection=INSPECTION)
    assert not Image.objects.exists()
    assert not stored_files(settings)


def test_service_batch_copy_counts_actual_cumulative_bytes(settings):
    settings.UPLOAD_MAX_BATCH_BYTES = 2 * len(PNG) - 1
    uploads = [png_upload("one.png"), png_upload("two.png")]
    for upload in uploads:
        upload.size = 1
    with pytest.raises(UploadLimitError):
        create_uploaded_media_batch(uploads, inspections=[INSPECTION, INSPECTION])
    assert not Image.objects.exists()
    assert not stored_files(settings)


def test_exclusive_publication_never_replaces_an_existing_original(
    settings, monkeypatch
):
    monkeypatch.setattr(
        "pandect.catalog.services.uuid4", lambda: SimpleNamespace(hex="fixed-for-test")
    )
    directory = Path(settings.MEDIA_ROOT) / "catalog/images"
    directory.mkdir(parents=True)
    original = directory / "fixed-for-test.png"
    original.write_bytes(b"existing canonical bytes")
    with pytest.raises(StorageCapacityError):
        create_uploaded_media(png_upload(), inspection=INSPECTION)
    assert original.read_bytes() == b"existing canonical bytes"
    assert stored_files(settings) == [original]
    assert not CatalogItem.objects.exists()


def test_busy_probe_returns_retryable_response_without_partial_batch(
    client, settings, monkeypatch
):
    from pandect.media_runner.client import RunnerBusy

    monkeypatch.setattr(
        "pandect.catalog.services._run_ffprobe", Mock(side_effect=RunnerBusy("busy"))
    )
    mp4 = SimpleUploadedFile(
        "synthetic.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 12
    )
    response = client.post(
        reverse("catalogue-bulk-add"), {"files": [png_upload(), mp4]}
    )
    assert response.status_code == 503
    assert response["Retry-After"] == "30"
    assert b"Media processing is busy" in response.content
    assert not CatalogItem.objects.exists()
    assert not stored_files(settings)


@pytest.mark.parametrize("item_type", [None, "image", "video", "audio"])
def test_intake_displays_runner_availability(client, monkeypatch, item_type):
    monkeypatch.setattr("pandect.catalog.views.runner_status", lambda: {"busy": True})
    url = (
        reverse("catalogue-add-item", kwargs={"item_type": item_type})
        if item_type
        else reverse("catalogue-bulk-add")
    )
    response = client.get(url)
    assert response.status_code == 200
    assert b"Media processing is currently busy" in response.content
    assert b"requiring a probe" in response.content


def test_unavailable_probe_returns_retryable_safe_message(client, monkeypatch):
    from pandect.media_runner.client import RunnerUnavailable

    monkeypatch.setattr(
        "pandect.catalog.services._run_ffprobe",
        Mock(side_effect=RunnerUnavailable("private socket details")),
    )
    mp4 = SimpleUploadedFile(
        "synthetic.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 12
    )
    response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "video"}), {"file": mp4}
    )
    assert response.status_code == 503
    assert response["Retry-After"] == "30"
    assert b"private socket details" not in response.content
    assert not CatalogItem.objects.exists()

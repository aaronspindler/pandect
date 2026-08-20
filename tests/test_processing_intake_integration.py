"""Intake-to-processing transaction boundary tests."""

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from pandect.catalog.services import create_catalogued_message, create_uploaded_media
from pandect.messages.models import TextMessage
from pandect.processing.models import ProcessingRun
from pandect.processing.services import enqueue_ready_stages

pytestmark = pytest.mark.django_db


def test_media_intake_creates_durable_work_and_enqueues_only_after_commit(
    django_capture_on_commit_callbacks,
):
    upload = SimpleUploadedFile(
        "pixel.png",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1).to_bytes(4, "big") * 2,
    )
    with patch(
        "pandect.processing.services.enqueue_ready_stages",
        wraps=enqueue_ready_stages,
    ) as enqueue:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            image = create_uploaded_media(upload)

        run = ProcessingRun.objects.get(catalog_item=image.catalog_item)
        enqueue.assert_not_called()
        assert len(callbacks) == 1
        callbacks[0]()

    enqueue.assert_called_once_with(str(run.pk))


def test_media_intake_stays_committed_when_post_commit_dispatch_fails(
    django_capture_on_commit_callbacks,
    caplog,
):
    original = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1).to_bytes(4, "big") * 2
    upload = SimpleUploadedFile("private-pixel.png", original)

    with patch(
        "pandect.processing.services.enqueue_ready_stages",
        side_effect=RuntimeError("sensitive dispatch detail"),
    ):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            image = create_uploaded_media(upload)
        callbacks[0]()

    run = ProcessingRun.objects.get(catalog_item=image.catalog_item)
    assert run.stages.filter(status="pending").exists()
    assert type(image).objects.filter(pk=image.pk).exists()
    with image.file.open("rb") as stream:
        assert stream.read() == original
    assert "private-pixel.png" not in caplog.text
    assert "sensitive dispatch detail" not in caplog.text
    assert str(run.pk) in caplog.text


def test_message_intake_rolls_back_run_and_enqueue_callback_with_catalogue_item(
    django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                create_catalogued_message(
                    TextMessage,
                    message_fields={"body": "rolled back evidence"},
                )
                raise RuntimeError("force rollback")

    assert ProcessingRun.objects.count() == 0
    assert callbacks == []


def test_blank_media_caption_does_not_duplicate_processing_of_referenced_media():
    upload = SimpleUploadedFile(
        "pixel.png",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1).to_bytes(4, "big") * 2,
    )
    image = create_uploaded_media(upload)

    from pandect.messages.models import ImageMessage

    message = create_catalogued_message(
        ImageMessage,
        message_fields={"body": ""},
        subtype_fields={"image": image},
    )

    assert ProcessingRun.objects.filter(catalog_item=image.catalog_item).count() == 1
    assert not ProcessingRun.objects.filter(
        catalog_item=message.message.catalog_item
    ).exists()

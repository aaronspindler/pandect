from datetime import datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.catalog.services import (
    create_catalogued_item,
    create_catalogued_message,
)
from pandect.events.models import Event
from pandect.messages.models import (
    CommunicationParticipant,
    ImageMessage,
    Message,
    MessageParticipant,
    TextMessage,
    VideoMessage,
)
from pandect.notes.models import Note
from pandect.photos.models import Image
from pandect.videos.models import Video


def create_item(item_type, **fields):
    return CatalogItem.objects.create(item_type=item_type, **fields)


def create_text_message(body="Message body", **message_fields):
    text_message = create_catalogued_message(
        TextMessage,
        message_fields={"body": body, **message_fields},
    )
    return text_message.message


@pytest.mark.django_db
def test_each_catalog_item_type_can_be_created_and_loaded_explicitly():
    image = create_catalogued_item(
        Image,
        item_fields={"title": "Still image"},
        extension_fields={
            "file": "catalog/images/still.jpg",
            "mime_type": "image/jpeg",
            "original_filename": "still.jpg",
            "width": 1920,
            "height": 1080,
        },
    )
    image_item = image.catalog_item
    video = create_catalogued_item(
        Video,
        item_fields={"title": "Clip"},
        extension_fields={
            "file": "catalog/videos/clip.mp4",
            "mime_type": "video/mp4",
            "original_filename": "clip.mp4",
            "duration": timedelta(seconds=8),
            "width": 1280,
            "height": 720,
        },
    )
    video_item = video.catalog_item
    event = create_catalogued_item(
        Event,
        item_fields={"title": "Observed event"},
        extension_fields={"description": "Description"},
    )
    event_item = event.catalog_item
    note = create_catalogued_item(
        Note,
        item_fields={"title": "Operator note"},
        extension_fields={"body": "An observation"},
    )
    note_item = note.catalog_item
    text_message = create_text_message()

    image_message = create_catalogued_message(
        ImageMessage,
        message_fields={"body": "Optional image caption"},
        subtype_fields={"image": image},
    )
    image_message_item = image_message.message.catalog_item
    video_message = create_catalogued_message(
        VideoMessage,
        message_fields={"body": "Optional video caption"},
        subtype_fields={"video": video},
    )
    video_message_item = video_message.message.catalog_item

    assert image_item.image == image
    assert video_item.video == video
    assert event_item.event == event
    assert note_item.note == note
    assert text_message.text_message.message == text_message
    assert image_message_item.message.image_message == image_message
    assert video_message_item.message.video_message == video_message
    assert CatalogItem.objects.count() == 7


@pytest.mark.django_db
def test_catalog_items_are_ordered_chronologically_with_unknown_times_last():
    now = timezone.now()
    later = create_item(
        CatalogItem.ItemType.EVENT,
        occurred_at=now + timedelta(hours=1),
    )
    unknown = create_item(CatalogItem.ItemType.NOTE, occurred_at=None)
    earlier = create_item(
        CatalogItem.ItemType.IMAGE,
        occurred_at=now - timedelta(hours=1),
    )

    assert list(CatalogItem.objects.values_list("pk", flat=True)) == [
        earlier.pk,
        later.pk,
        unknown.pk,
    ]


@pytest.mark.django_db
def test_messages_and_concrete_message_types_are_independently_queryable():
    image = Image.objects.create(catalog_item=create_item(CatalogItem.ItemType.IMAGE))
    video = Video.objects.create(catalog_item=create_item(CatalogItem.ItemType.VIDEO))
    text = create_text_message()
    image_base = Message.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.IMAGE_MESSAGE)
    )
    image_message = ImageMessage.objects.create(message=image_base, image=image)
    video_base = Message.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.VIDEO_MESSAGE)
    )
    video_message = VideoMessage.objects.create(message=video_base, video=video)

    assert set(Message.objects.values_list("pk", flat=True)) == {
        text.pk,
        image_base.pk,
        video_base.pk,
    }
    assert list(TextMessage.objects.values_list("pk", flat=True)) == [text.pk]
    assert list(ImageMessage.objects.values_list("pk", flat=True)) == [image_message.pk]
    assert list(VideoMessage.objects.values_list("pk", flat=True)) == [video_message.pk]


@pytest.mark.django_db
def test_message_supports_sender_and_group_recipients():
    sender = CommunicationParticipant.objects.create(
        display_name="Sender",
        identifier="sender@example.test",
        participant_type=CommunicationParticipant.ParticipantType.PERSON,
    )
    first_receiver = CommunicationParticipant.objects.create(
        display_name="First receiver"
    )
    second_receiver = CommunicationParticipant.objects.create(
        identifier="second@example.test",
        participant_type=CommunicationParticipant.ParticipantType.ACCOUNT,
    )
    message = create_text_message(sender=sender)
    MessageParticipant.objects.create(
        message=message,
        participant=first_receiver,
        role=MessageParticipant.Role.TO,
    )
    MessageParticipant.objects.create(
        message=message,
        participant=second_receiver,
        role=MessageParticipant.Role.CC,
    )

    links = list(message.recipients.select_related("participant"))
    assert message.sender == sender
    assert {link.participant for link in links} == {
        first_receiver,
        second_receiver,
    }


@pytest.mark.django_db
def test_unknown_message_participants_and_missing_participants_are_supported():
    placeholder = CommunicationParticipant.objects.create()
    message = create_text_message()

    assert placeholder.display_name == ""
    assert placeholder.identifier == ""
    assert placeholder.participant_type == (
        CommunicationParticipant.ParticipantType.UNKNOWN
    )
    assert message.sender is None
    assert not message.recipients.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["to", "cc", "bcc", "mentioned", "unknown"])
def test_each_recipient_role_is_persisted(role):
    participant = CommunicationParticipant.objects.create(display_name="Recipient")
    link = MessageParticipant.objects.create(
        message=create_text_message(),
        participant=participant,
        role=role,
    )

    link.refresh_from_db()
    assert link.role == role


@pytest.mark.django_db
def test_participant_may_have_different_roles_but_not_duplicate_a_role():
    participant = CommunicationParticipant.objects.create(display_name="Recipient")
    message = create_text_message()
    MessageParticipant.objects.create(
        message=message,
        participant=participant,
        role=MessageParticipant.Role.TO,
    )
    MessageParticipant.objects.create(
        message=message,
        participant=participant,
        role=MessageParticipant.Role.MENTIONED,
    )

    with pytest.raises(ValidationError):
        MessageParticipant.objects.create(
            message=message,
            participant=participant,
            role=MessageParticipant.Role.TO,
        )

    duplicate = MessageParticipant(
        message=message,
        participant=participant,
        role=MessageParticipant.Role.TO,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MessageParticipant.objects.bulk_create([duplicate])


@pytest.mark.django_db
@pytest.mark.parametrize("body", ["", "   ", "\n\t"])
def test_text_message_save_requires_non_whitespace_body(body):
    item_count = CatalogItem.objects.count()
    with pytest.raises(ValidationError, match="non-empty body"):
        create_catalogued_message(
            TextMessage,
            message_fields={"body": body},
        )
    assert CatalogItem.objects.count() == item_count


@pytest.mark.django_db
def test_existing_text_message_cannot_be_changed_to_whitespace():
    message = create_text_message("Original body")
    message.body = "   "

    with pytest.raises(ValidationError, match="non-empty body"):
        message.save()

    message.refresh_from_db()
    assert message.body == "Original body"


@pytest.mark.django_db
def test_note_save_requires_non_whitespace_body():
    with pytest.raises(ValidationError):
        Note.objects.create(
            catalog_item=create_item(CatalogItem.ItemType.NOTE),
            body="   ",
        )


@pytest.mark.django_db
def test_media_messages_allow_empty_caption_and_reuse_standalone_media():
    image = Image.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.IMAGE),
        original_filename="image.png",
    )
    video = Video.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.VIDEO),
        original_filename="video.mp4",
    )
    image_base = Message.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.IMAGE_MESSAGE),
        body="",
    )
    video_base = Message.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.VIDEO_MESSAGE),
        body="",
    )

    image_message = ImageMessage.objects.create(message=image_base, image=image)
    video_message = VideoMessage.objects.create(message=video_base, video=video)

    assert image_message.image == image
    assert video_message.video == video
    assert Image.objects.get(pk=image.pk).original_filename == "image.png"
    assert Video.objects.get(pk=video.pk).original_filename == "video.mp4"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("media_model", "subtype_model", "media_type", "message_type", "field_name"),
    [
        (
            Image,
            ImageMessage,
            CatalogItem.ItemType.IMAGE,
            CatalogItem.ItemType.IMAGE_MESSAGE,
            "image",
        ),
        (
            Video,
            VideoMessage,
            CatalogItem.ItemType.VIDEO,
            CatalogItem.ItemType.VIDEO_MESSAGE,
            "video",
        ),
    ],
)
def test_shared_media_is_protected_and_survives_message_deletion(
    media_model,
    subtype_model,
    media_type,
    message_type,
    field_name,
):
    media_item = create_item(media_type)
    media = media_model.objects.create(catalog_item=media_item)
    message_item = create_item(message_type)
    message = Message.objects.create(catalog_item=message_item)
    subtype_model.objects.create(
        message=message,
        **{field_name: media},
    )

    with pytest.raises(ProtectedError):
        media_item.delete()

    with pytest.raises(TypeError, match="through CatalogItem"):
        message.delete()
    with pytest.raises(TypeError, match="through CatalogItem"):
        subtype_model.objects.filter(pk=message.pk).delete()

    message_item_id = message_item.pk
    media_item_id = media_item.pk
    message_item.delete()

    assert not CatalogItem.objects.filter(pk=message_item_id).exists()
    assert media_model.objects.filter(pk=media.pk).exists()
    assert CatalogItem.objects.filter(pk=media_item_id).exists()

    with pytest.raises(TypeError, match="through CatalogItem"):
        media.delete()
    media_item.delete()
    assert not CatalogItem.objects.filter(pk=media_item_id).exists()


@pytest.mark.django_db
def test_reply_relationships_reject_self_reply_and_null_on_target_deletion():
    original = create_text_message("Original")
    reply = create_text_message("Reply", reply_to=original)

    assert list(original.replies.all()) == [reply]

    reply.reply_to = reply
    with pytest.raises(ValidationError, match="cannot reply to itself"):
        reply.save()

    with pytest.raises(IntegrityError), transaction.atomic():
        Message.objects.filter(pk=reply.pk).update(reply_to=reply)

    reply.refresh_from_db()
    original.catalog_item.delete()
    reply.refresh_from_db()
    assert reply.reply_to is None


@pytest.mark.django_db
def test_timestamp_meanings_remain_distinct_and_timezone_aware():
    now = timezone.now()
    occurred_at = now - timedelta(days=2)
    catalogued_at = now - timedelta(days=1)
    item = create_item(
        CatalogItem.ItemType.EVENT,
        occurred_at=occurred_at,
        catalogued_at=catalogued_at,
    )
    initial_created_at = item.created_at
    initial_updated_at = item.updated_at
    item.title = "Updated title"
    item.save()
    item.refresh_from_db()

    assert item.occurred_at == occurred_at
    assert item.catalogued_at == catalogued_at
    assert item.created_at == initial_created_at
    assert item.updated_at > initial_updated_at
    assert all(
        timezone.is_aware(value)
        for value in (
            item.occurred_at,
            item.catalogued_at,
            item.created_at,
            item.updated_at,
        )
    )


@pytest.mark.django_db
def test_unknown_occurred_at_is_not_replaced_with_catalogued_at():
    item = create_item(CatalogItem.ItemType.NOTE)

    assert item.occurred_at is None
    assert item.catalogued_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize("field_name", ["occurred_at", "catalogued_at"])
def test_catalog_item_rejects_naive_timestamps(field_name):
    with pytest.raises(ValidationError, match="timezone information"):
        create_item(
            CatalogItem.ItemType.EVENT,
            **{field_name: datetime(2026, 8, 12, 12, 0)},
        )


@pytest.mark.django_db
def test_event_rejects_naive_end_timestamp():
    with pytest.raises(ValidationError, match="timezone information"):
        Event.objects.create(
            catalog_item=create_item(CatalogItem.ItemType.EVENT),
            ended_at=datetime(2026, 8, 12, 12, 0),
        )


@pytest.mark.django_db
def test_event_cannot_end_before_it_occurs_or_move_after_its_end():
    occurred_at = timezone.now()
    item = create_item(CatalogItem.ItemType.EVENT, occurred_at=occurred_at)

    with pytest.raises(ValidationError, match="cannot end before"):
        Event.objects.create(
            catalog_item=item,
            ended_at=occurred_at - timedelta(seconds=1),
        )

    event = Event.objects.create(
        catalog_item=item,
        ended_at=occurred_at + timedelta(hours=1),
    )
    item.occurred_at = event.ended_at + timedelta(seconds=1)
    with pytest.raises(ValidationError, match="cannot occur after"):
        item.save()

    current_item = CatalogItem.objects.get(pk=item.pk)
    current_item.occurred_at = occurred_at + timedelta(minutes=30)
    current_item.save()
    stale_event = Event.objects.select_related("catalog_item").get(pk=item.pk)
    stale_event.catalog_item.occurred_at = occurred_at
    stale_event.ended_at = occurred_at + timedelta(minutes=15)
    with pytest.raises(ValidationError, match="cannot end before"):
        stale_event.save()


@pytest.mark.django_db
def test_catalog_item_type_cannot_change_after_creation():
    item = create_item(CatalogItem.ItemType.IMAGE)
    Image.objects.create(catalog_item=item)
    item.item_type = CatalogItem.ItemType.VIDEO

    with pytest.raises(ValidationError, match="type cannot be changed"):
        item.save()

    item.refresh_from_db()
    assert item.item_type == CatalogItem.ItemType.IMAGE

    replacement = CatalogItem(
        pk=item.pk,
        item_type=CatalogItem.ItemType.VIDEO,
        catalogued_at=item.catalogued_at,
    )
    with pytest.raises(ValidationError, match="type cannot be changed"):
        replacement.save(update_fields={"item_type"})

    item.item_type = CatalogItem.ItemType.VIDEO
    with pytest.raises(ValidationError, match="Catalog item type"):
        Video.objects.create(catalog_item=item)


@pytest.mark.django_db
def test_extension_models_reject_all_mismatched_catalog_item_types():
    image = Image.objects.create(catalog_item=create_item(CatalogItem.ItemType.IMAGE))
    video = Video.objects.create(catalog_item=create_item(CatalogItem.ItemType.VIDEO))

    invalid_extensions = [
        Image(catalog_item=create_item(CatalogItem.ItemType.VIDEO)),
        Video(catalog_item=create_item(CatalogItem.ItemType.IMAGE)),
        Event(catalog_item=create_item(CatalogItem.ItemType.NOTE)),
        Note(
            catalog_item=create_item(CatalogItem.ItemType.EVENT),
            body="Observation",
        ),
        Message(catalog_item=create_item(CatalogItem.ItemType.IMAGE)),
    ]
    for extension in invalid_extensions:
        with pytest.raises(ValidationError, match="Catalog item type"):
            extension.save()

    image_base = Message.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.IMAGE_MESSAGE)
    )
    video_base = Message.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.VIDEO_MESSAGE)
    )
    invalid_subtypes = [
        TextMessage(message=image_base),
        ImageMessage(message=video_base, image=image),
        VideoMessage(message=image_base, video=video),
    ]
    for subtype in invalid_subtypes:
        with pytest.raises(ValidationError, match="Catalog item type"):
            subtype.save()

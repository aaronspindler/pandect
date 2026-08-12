"""Atomic construction for multi-table catalogue records."""

from django.db import transaction

from pandect.catalog.models import CatalogItem
from pandect.events.models import Event
from pandect.messages.models import ImageMessage, Message, TextMessage, VideoMessage
from pandect.notes.models import Note
from pandect.photos.models import Image
from pandect.videos.models import Video

_ITEM_TYPES = {
    Image: CatalogItem.ItemType.IMAGE,
    Video: CatalogItem.ItemType.VIDEO,
    Event: CatalogItem.ItemType.EVENT,
    Note: CatalogItem.ItemType.NOTE,
}

_MESSAGE_TYPES = {
    TextMessage: CatalogItem.ItemType.TEXT_MESSAGE,
    ImageMessage: CatalogItem.ItemType.IMAGE_MESSAGE,
    VideoMessage: CatalogItem.ItemType.VIDEO_MESSAGE,
}


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
    return model.objects.create(message=message, **(subtype_fields or {}))

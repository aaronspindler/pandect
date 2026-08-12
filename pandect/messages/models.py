"""Source-neutral catalogued communication records."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models

from pandect.catalog.models import (
    CatalogItem,
    RootedExtension,
    ValidatedModel,
    validate_catalog_item_type,
    validate_non_whitespace,
)


class CommunicationParticipant(ValidatedModel):
    """An identity as shown in one communication context, not a resolved person."""

    class ParticipantType(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        PERSON = "person", "Person"
        ACCOUNT = "account", "Account"
        GROUP = "group", "Group"
        SYSTEM = "system", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    display_name = models.CharField(max_length=255, blank=True)
    identifier = models.CharField(max_length=512, blank=True)
    participant_type = models.CharField(
        max_length=16,
        choices=ParticipantType.choices,
        default=ParticipantType.UNKNOWN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return (
            self.display_name
            or self.identifier
            or f"Unknown participant {str(self.pk)[:8]}"
        )


class Message(RootedExtension):
    """Fields shared by every catalogued communication message."""

    MESSAGE_ITEM_TYPES = {
        CatalogItem.ItemType.TEXT_MESSAGE,
        CatalogItem.ItemType.IMAGE_MESSAGE,
        CatalogItem.ItemType.VIDEO_MESSAGE,
    }

    catalog_item = models.OneToOneField(
        CatalogItem,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="message",
    )
    body = models.TextField(blank=True)
    sender = models.ForeignKey(
        CommunicationParticipant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_messages",
    )
    reply_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(reply_to__isnull=True)
                | ~models.Q(reply_to=models.F("catalog_item")),
                name="messages_message_not_self_reply",
            )
        ]

    def __str__(self) -> str:
        return str(self.catalog_item)

    def clean(self) -> None:
        super().clean()
        if self.catalog_item_id:
            validate_catalog_item_type(self.catalog_item, self.MESSAGE_ITEM_TYPES)
            if self.catalog_item.item_type == CatalogItem.ItemType.TEXT_MESSAGE:
                try:
                    validate_non_whitespace(self.body)
                except ValidationError as error:
                    raise ValidationError(
                        {"body": "A text message requires a non-empty body."}
                    ) from error
        if self.reply_to_id is not None and self.reply_to_id == self.catalog_item_id:
            raise ValidationError({"reply_to": "A message cannot reply to itself."})


class MessageParticipant(ValidatedModel):
    """A recipient and their role in a message."""

    class Role(models.TextChoices):
        TO = "to", "To"
        CC = "cc", "Cc"
        BCC = "bcc", "Bcc"
        MENTIONED = "mentioned", "Mentioned"
        UNKNOWN = "unknown", "Unknown"

    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="recipients",
    )
    participant = models.ForeignKey(
        CommunicationParticipant,
        on_delete=models.CASCADE,
        related_name="message_recipient_links",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.TO)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["message", "participant", "role"],
                name="messages_unique_message_participant_role",
            )
        ]

    def __str__(self) -> str:
        return f"{self.participant} ({self.get_role_display()})"


class TextMessage(RootedExtension):
    """A message whose primary content is non-empty text."""

    message = models.OneToOneField(
        Message,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="text_message",
    )

    def __str__(self) -> str:
        return str(self.message)

    def clean(self) -> None:
        super().clean()
        if not self.message_id:
            return
        validate_catalog_item_type(
            self.message.catalog_item,
            {CatalogItem.ItemType.TEXT_MESSAGE},
        )


class ImageMessage(RootedExtension):
    """A message whose primary content references a standalone image."""

    message = models.OneToOneField(
        Message,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="image_message",
    )
    image = models.ForeignKey(
        "photos.Image",
        on_delete=models.PROTECT,
        related_name="message_uses",
    )

    def __str__(self) -> str:
        return str(self.message)

    def clean(self) -> None:
        super().clean()
        if self.message_id:
            validate_catalog_item_type(
                self.message.catalog_item,
                {CatalogItem.ItemType.IMAGE_MESSAGE},
            )


class VideoMessage(RootedExtension):
    """A message whose primary content references a standalone video."""

    message = models.OneToOneField(
        Message,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="video_message",
    )
    video = models.ForeignKey(
        "videos.Video",
        on_delete=models.PROTECT,
        related_name="message_uses",
    )

    def __str__(self) -> str:
        return str(self.message)

    def clean(self) -> None:
        super().clean()
        if self.message_id:
            validate_catalog_item_type(
                self.message.catalog_item,
                {CatalogItem.ItemType.VIDEO_MESSAGE},
            )

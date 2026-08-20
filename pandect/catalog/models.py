"""Shared source-neutral catalogue records and model behaviour."""

import uuid

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def validate_non_whitespace(value: str) -> None:
    """Reject text that is empty after trimming surrounding whitespace."""
    if not value or not value.strip():
        raise ValidationError(
            "This value must contain at least one non-whitespace character.",
            code="blank",
        )


def validate_timezone_aware(value) -> None:
    """Reject naive datetimes rather than silently assigning a timezone."""
    if value is not None and timezone.is_naive(value):
        raise ValidationError(
            "Datetime values must include timezone information.",
            code="naive_datetime",
        )


class ValidatedModel(models.Model):
    """Run Django model validation for ordinary catalogue writes."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class RootedExtensionQuerySet(models.QuerySet):
    """Prevent bulk deletion from bypassing the catalogue root."""

    def delete(self):
        raise TypeError("Delete extension records through CatalogItem.")


class RootedExtension(ValidatedModel):
    """Require extensions to be deleted through their catalogue root."""

    objects = RootedExtensionQuerySet.as_manager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        raise TypeError("Delete extension records through CatalogItem.")


class CanonicalFileExtensionQuerySet(RootedExtensionQuerySet):
    """Keep a media extension's canonical storage pointer create-only."""

    def update(self, **kwargs):
        if "file" in kwargs:
            raise TypeError("A canonical media file pointer is immutable.")
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if "file" in fields:
            raise TypeError("A canonical media file pointer is immutable.")
        return super().bulk_update(objs, fields, batch_size=batch_size)


class CanonicalFileExtension(RootedExtension):
    """Rooted media extension whose original file cannot be repointed."""

    objects = CanonicalFileExtensionQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored_name = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("file", flat=True)
                .get()
            )
            current_file = self.file
            current_name = getattr(current_file, "name", current_file) or ""
            if current_name != stored_name:
                raise TypeError("A canonical media file pointer is immutable.")
        return super().save(*args, **kwargs)


class CatalogItem(ValidatedModel):
    """The shared chronological record for every independently catalogued item."""

    class ItemType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        AUDIO = "audio", "Audio"
        EVENT = "event", "Event"
        NOTE = "note", "Note"
        TEXT_MESSAGE = "text_message", "Text message"
        IMAGE_MESSAGE = "image_message", "Image message"
        VIDEO_MESSAGE = "video_message", "Video message"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item_type = models.CharField(max_length=32, choices=ItemType.choices)
    title = models.CharField(max_length=255, blank=True)
    occurred_at = models.DateTimeField(
        null=True,
        blank=True,
        validators=[validate_timezone_aware],
    )
    catalogued_at = models.DateTimeField(
        default=timezone.now,
        validators=[validate_timezone_aware],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [
            models.F("occurred_at").asc(nulls_last=True),
            "catalogued_at",
            "id",
        ]
        indexes = [
            models.Index(
                fields=["occurred_at", "catalogued_at"],
                name="catalog_item_time_idx",
            ),
            models.Index(
                fields=["item_type", "occurred_at"],
                name="catalog_type_time_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.title or f"{self.get_item_type_display()} {self.pk}"

    def clean(self) -> None:
        super().clean()
        previous_type = (
            type(self)
            .objects.filter(pk=self.pk)
            .values_list("item_type", flat=True)
            .first()
        )
        if previous_type is None:
            return
        if self.item_type != previous_type:
            raise ValidationError({"item_type": "An item's type cannot be changed."})

        if self.item_type != self.ItemType.EVENT or self.occurred_at is None:
            return
        event_model = django_apps.get_model("events", "Event")
        ended_at = (
            event_model.objects.filter(pk=self.pk)
            .values_list("ended_at", flat=True)
            .first()
        )
        if (
            ended_at is not None
            and timezone.is_aware(self.occurred_at)
            and timezone.is_aware(ended_at)
            and ended_at < self.occurred_at
        ):
            raise ValidationError(
                {"occurred_at": "An event cannot occur after it ends."}
            )


def validate_catalog_item_type(
    catalog_item: CatalogItem,
    expected_types: set[CatalogItem.ItemType],
) -> None:
    item_type = (
        CatalogItem.objects.filter(pk=catalog_item.pk)
        .values_list("item_type", flat=True)
        .first()
    )
    if item_type not in expected_types:
        expected = ", ".join(sorted(item_type.value for item_type in expected_types))
        raise ValidationError(
            {"catalog_item": (f"Catalog item type must be one of: {expected}.")}
        )

"""Source-neutral catalogued event records."""

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from pandect.catalog.models import (
    CatalogItem,
    RootedExtension,
    validate_catalog_item_type,
    validate_timezone_aware,
)


class Event(RootedExtension):
    """A catalogued occurrence with an optional end time."""

    catalog_item = models.OneToOneField(
        CatalogItem,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="event",
    )
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        validators=[validate_timezone_aware],
    )
    description = models.TextField(blank=True)

    def __str__(self) -> str:
        return str(self.catalog_item)

    def clean(self) -> None:
        super().clean()
        if not self.catalog_item_id:
            return
        validate_catalog_item_type(
            self.catalog_item,
            {CatalogItem.ItemType.EVENT},
        )
        occurred_at = (
            CatalogItem.objects.filter(pk=self.catalog_item_id)
            .values_list("occurred_at", flat=True)
            .first()
        )
        if (
            self.ended_at is not None
            and occurred_at is not None
            and timezone.is_aware(self.ended_at)
            and timezone.is_aware(occurred_at)
            and self.ended_at < occurred_at
        ):
            raise ValidationError({"ended_at": "An event cannot end before it occurs."})

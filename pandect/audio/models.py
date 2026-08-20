"""Source-neutral catalogued audio records."""

from datetime import timedelta

from django.db import models

from pandect.catalog.models import (
    CanonicalFileExtension,
    CatalogItem,
    validate_catalog_item_type,
)


class Audio(CanonicalFileExtension):
    """A standalone local audio recording."""

    catalog_item = models.OneToOneField(
        CatalogItem,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="audio",
    )
    file = models.FileField(upload_to="catalog/audio/", max_length=500, blank=True)
    mime_type = models.CharField(max_length=255, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    duration = models.DurationField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(duration__isnull=True)
                | models.Q(duration__gte=timedelta(0)),
                name="audio_audio_duration_nonnegative",
            )
        ]

    def __str__(self) -> str:
        return str(self.catalog_item)

    def clean(self) -> None:
        super().clean()
        if self.catalog_item_id:
            validate_catalog_item_type(
                self.catalog_item,
                {CatalogItem.ItemType.AUDIO},
            )

"""Source-neutral catalogued video records."""

from datetime import timedelta

from django.db import models

from pandect.catalog.models import (
    CanonicalFileExtension,
    CatalogItem,
    validate_catalog_item_type,
)


class Video(CanonicalFileExtension):
    """A standalone local video that may also be used by a video message."""

    catalog_item = models.OneToOneField(
        CatalogItem,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="video",
    )
    file = models.FileField(upload_to="catalog/videos/", max_length=500, blank=True)
    mime_type = models.CharField(max_length=255, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    duration = models.DurationField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(duration__isnull=True)
                | models.Q(duration__gte=timedelta(0)),
                name="videos_video_duration_nonnegative",
            )
        ]

    def __str__(self) -> str:
        return str(self.catalog_item)

    def clean(self) -> None:
        super().clean()
        if self.catalog_item_id:
            validate_catalog_item_type(
                self.catalog_item,
                {CatalogItem.ItemType.VIDEO},
            )

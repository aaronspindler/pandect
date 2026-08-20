"""Source-neutral catalogued photo records."""

from django.db import models

from pandect.catalog.models import (
    CanonicalFileExtension,
    CatalogItem,
    validate_catalog_item_type,
)


class Image(CanonicalFileExtension):
    """A standalone local image that may also be used by an image message."""

    catalog_item = models.OneToOneField(
        CatalogItem,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="image",
    )
    file = models.FileField(upload_to="catalog/images/", max_length=500, blank=True)
    mime_type = models.CharField(max_length=255, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return str(self.catalog_item)

    def clean(self) -> None:
        super().clean()
        if self.catalog_item_id:
            validate_catalog_item_type(
                self.catalog_item,
                {CatalogItem.ItemType.IMAGE},
            )

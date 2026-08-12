"""Source-neutral catalogued note records."""

from django.db import models

from pandect.catalog.models import (
    CatalogItem,
    RootedExtension,
    validate_catalog_item_type,
    validate_non_whitespace,
)


class Note(RootedExtension):
    """A catalogued plain-text observation."""

    catalog_item = models.OneToOneField(
        CatalogItem,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="note",
    )
    body = models.TextField(validators=[validate_non_whitespace])

    def __str__(self) -> str:
        return str(self.catalog_item)

    def clean(self) -> None:
        super().clean()
        if self.catalog_item_id:
            validate_catalog_item_type(
                self.catalog_item,
                {CatalogItem.ItemType.NOTE},
            )

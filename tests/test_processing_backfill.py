"""Tests for bounded processing backfill selection."""

import pytest
from django.core.management import CommandError, call_command

from pandect.catalog.models import CatalogItem
from pandect.catalog.services import create_catalogued_item
from pandect.notes.models import Note
from pandect.processing.models import ProcessingRun

pytestmark = pytest.mark.django_db


def test_backfill_requires_an_explicit_bounded_selection():
    with pytest.raises(CommandError, match="item IDs"):
        call_command("backfill_processing")


def test_backfill_skips_unsupported_items_without_failing(capsys):
    note = create_catalogued_item(Note, extension_fields={"body": "local note"})

    call_command("backfill_processing", str(note.pk), limit=1)

    assert ProcessingRun.objects.count() == 0
    assert "skipped 1" in capsys.readouterr().out


def test_backfill_dry_run_does_not_create_work(capsys):
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.TEXT_MESSAGE)

    call_command(
        "backfill_processing",
        str(item.pk),
        limit=1,
        dry_run=True,
    )

    assert ProcessingRun.objects.count() == 0
    assert "Would create 1" in capsys.readouterr().out

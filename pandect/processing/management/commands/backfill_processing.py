"""Create bounded, versioned local processing runs for existing items."""

from django.core.management.base import BaseCommand, CommandError

from pandect.catalog.models import CatalogItem
from pandect.processing.models import ProcessingRun
from pandect.processing.services import ProcessingError, create_local_run


class Command(BaseCommand):
    help = "Create local processing runs for selected catalogue items."

    def add_arguments(self, parser):
        parser.add_argument("item_ids", nargs="*")
        parser.add_argument(
            "--item-type",
            action="append",
            choices=CatalogItem.ItemType.values,
            dest="item_types",
        )
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000.")
        if not options["item_ids"] and not options["item_types"]:
            raise CommandError("Pass item IDs and/or at least one --item-type.")

        items = CatalogItem.objects.all()
        if options["item_ids"]:
            items = items.filter(pk__in=options["item_ids"])
        if options["item_types"]:
            items = items.filter(item_type__in=options["item_types"])
        selected = list(items.order_by("catalogued_at", "id")[:limit])
        if options["dry_run"]:
            self.stdout.write(f"Would create {len(selected)} processing run(s).")
            return

        created = 0
        skipped = 0
        for item in selected:
            try:
                create_local_run(item, trigger=ProcessingRun.Trigger.BACKFILL)
            except ProcessingError:
                skipped += 1
            else:
                created += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created} processing run(s); skipped {skipped}."
            )
        )

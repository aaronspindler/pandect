from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS
from django.db.models import F

from core.models import PendingFileDeletion
from core.storage import complete_file_deletion


class Command(BaseCommand):
    help = "Retry committed private-file deletions that have not been completed."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *, limit, dry_run, **options):
        if not 1 <= limit <= 10000:
            raise CommandError("Choose a limit between 1 and 10000.")
        pending = PendingFileDeletion.objects.order_by(
            F("last_attempted_at").asc(nulls_first=True), "created_at", "pk"
        )
        unscoped = pending.filter(case_id__isnull=True).count()
        if unscoped:
            self.stdout.write(
                f"{unscoped} pending deletions without a recorded case require review."
            )
        if dry_run:
            self.stdout.write(f"{pending.count()} pending private-file deletions.")
            return
        completed = sum(
            complete_file_deletion(item, DEFAULT_DB_ALIAS)
            for item in pending.filter(case_id__isnull=False)[:limit]
        )
        remaining = PendingFileDeletion.objects.count()
        self.stdout.write(f"Completed {completed} deletions. {remaining} pending.")

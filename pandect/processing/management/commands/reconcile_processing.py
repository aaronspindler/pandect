"""Bounded manual repair for abandoned stages and remote aggregation gaps."""

from django.core.management.base import BaseCommand, CommandError

from pandect.processing.identity_rematch import reconcile_gallery_rematch_jobs
from pandect.processing.remote.aggregation import finalize_ready_remote_aggregates
from pandect.processing.services import (
    DEFAULT_QUEUED_TIMEOUT_SECONDS,
    DEFAULT_RECONCILE_LIMIT,
    reconcile_stages,
)


class Command(BaseCommand):
    help = (
        "Recover stale durable stages, missing source replacements, and remote "
        "result aggregates."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_RECONCILE_LIMIT,
            help="Maximum number of stale and pending records to examine.",
        )
        parser.add_argument(
            "--queued-timeout",
            type=int,
            default=DEFAULT_QUEUED_TIMEOUT_SECONDS,
            help="Seconds before a queued stage is considered abandoned.",
        )

    def handle(self, *args, **options) -> None:
        if options["limit"] <= 0:
            raise CommandError("--limit must be positive")
        if options["queued_timeout"] <= 0:
            raise CommandError("--queued-timeout must be positive")
        report = reconcile_stages(
            limit=options["limit"],
            queued_timeout_seconds=options["queued_timeout"],
        )
        remote_aggregates = finalize_ready_remote_aggregates(limit=options["limit"])
        gallery_jobs = reconcile_gallery_rematch_jobs(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                "Processing reconciliation complete: "
                f"queued recovered={report.queued_recovered}, "
                f"running recovered={report.running_recovered}, "
                f"running failed={report.running_failed}, "
                f"source replacements repaired={report.replacements_repaired}, "
                f"pending runs examined={report.pending_examined}, "
                f"remote aggregates scheduled={remote_aggregates}, "
                f"gallery rematch jobs scheduled={gallery_jobs}."
            )
        )

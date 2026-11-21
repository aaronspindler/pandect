from time import monotonic, sleep
from uuid import UUID, uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django_tasks_db import DatabaseBackend
from django_tasks_db.models import DBTaskResult

from core.analysis.errors import ERROR_MESSAGES
from core.analysis.tasks import process_analysis
from core.models import AnalysisRun
from core.tasks import worker_probe


class Command(BaseCommand):
    help = "Check worker health or inspect the queue deliveries for one analysis run."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--timeout", type=int, default=30, help="Wait seconds (30).")
        mode.add_argument(
            "--analysis-run",
            type=UUID,
            help="Show the saved run status and its 10 most recent queue deliveries.",
        )

    def handle(self, *, timeout, analysis_run=None, **options):
        if analysis_run is not None:
            self.inspect_analysis_run(analysis_run)
            return
        if timeout <= 0:
            raise CommandError("--timeout must be a positive integer.")
        if not isinstance(worker_probe.get_backend(), DatabaseBackend):
            raise CommandError("The diagnostic requires the database Tasks backend.")

        nonce = uuid4().hex
        try:
            result = worker_probe.enqueue(nonce)
            deadline = monotonic() + timeout
            while True:
                result.refresh()
                if result.status == TaskResultStatus.SUCCESSFUL:
                    if result.return_value != nonce:
                        raise CommandError(
                            f"Worker probe {result.id} returned a mismatch."
                        )
                    self.stdout.write(
                        self.style.SUCCESS(f"Worker probe {result.id}: OK")
                    )
                    return
                if result.status == TaskResultStatus.FAILED:
                    raise CommandError(
                        f"Worker probe {result.id} failed. "
                        "Inspect the private task store."
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise CommandError(
                        f"Worker probe {result.id} timed out ({result.status}). "
                        "Check worker logs and database connectivity. "
                        "The probe remains in the task store."
                    )
                sleep(min(0.1, remaining))
        except (DatabaseError, TaskResultDoesNotExist) as error:
            raise CommandError(
                "Unable to check the worker. "
                "Check database connectivity and migrations."
            ) from error

    def inspect_analysis_run(self, run_id):
        try:
            run = AnalysisRun.objects.only("status", "error_code").get(pk=run_id)
            deliveries = list(
                DBTaskResult.objects.filter(
                    task_path=process_analysis.module_path,
                    args_kwargs={"args": [str(run.id)], "kwargs": {}},
                )
                .order_by("-enqueued_at", "-pk")
                .values("id", "status", "enqueued_at", "started_at", "finished_at")[:10]
            )
        except AnalysisRun.DoesNotExist:
            raise CommandError("Analysis run not found.") from None
        except DatabaseError as error:
            raise CommandError("Unable to read analysis and queue status.") from error

        code = run.error_code
        if code and code != "processing_failed" and code not in ERROR_MESSAGES:
            code = "unknown error"
        detail = f" ({code})" if code else ""
        self.stdout.write(f"Analysis run {run.id}: {run.status}{detail}")
        if not deliveries:
            self.stdout.write("No queue deliveries are still retained for this run.")
        for delivery in deliveries:
            self.stdout.write(
                f"Queue delivery {delivery['id']}: {delivery['status']} · "
                f"queued {delivery['enqueued_at'].isoformat()} · "
                f"started {delivery['started_at'] or '—'} · "
                f"finished {delivery['finished_at'] or '—'}"
            )

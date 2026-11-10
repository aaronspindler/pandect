from time import monotonic, sleep
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.tasks import TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django_tasks_db import DatabaseBackend

from core.tasks import worker_probe


class Command(BaseCommand):
    help = "Enqueue a synthetic probe and wait for a separate worker to complete it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout", type=int, default=30, help="Wait seconds (30)."
        )

    def handle(self, *, timeout, **options):
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

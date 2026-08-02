"""Bounded end-to-end smoke check for the configured Huey worker."""

import argparse
import math
import uuid

from django.core.management.base import BaseCommand, CommandError, CommandParser
from huey.exceptions import ResultTimeout

from pandect.core.tasks import echo


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
        raise argparse.ArgumentTypeError(
            "timeout must be greater than zero and no more than 300 seconds"
        )
    return timeout


class Command(BaseCommand):
    help = "Enqueue a side-effect-free task and verify its result."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--timeout",
            type=positive_timeout,
            default=10.0,
            metavar="SECONDS",
            help="Maximum seconds to wait for a worker result (default: 10).",
        )

    def handle(self, *args: object, **options: object) -> None:
        timeout = float(options["timeout"])
        expected = f"pandect-smoke-{uuid.uuid4().hex}"
        try:
            result = echo(expected)
            observed = result.get(blocking=True, timeout=timeout)
        except ResultTimeout as exc:
            raise CommandError(
                f"No Huey worker completed the task within {timeout:g} seconds. "
                "Confirm the worker is running and can access PANDECT_DATA_DIR."
            ) from exc
        except Exception as exc:
            raise CommandError(
                "The Huey task could not be enqueued or its result could not be read. "
                "Confirm the queue file is writable and the worker is healthy."
            ) from exc

        if observed != expected:
            raise CommandError("The Huey task returned an unexpected result.")

        self.stdout.write(self.style.SUCCESS("Huey task smoke check passed."))

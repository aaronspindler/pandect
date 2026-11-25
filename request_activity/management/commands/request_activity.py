"""Initialize, ingest, or check the operator request activity store."""

import os
import signal
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event
from time import monotonic

from django.core.management.base import BaseCommand, CommandError

from request_activity.spool import (
    STALE_HEARTBEAT_SECONDS,
    acknowledge,
    read_batch,
    read_health,
)
from request_activity.store import StoreUnavailable, Writer, initialize_schema

BATCH_SIZE = 250
POLL_SECONDS = 1
HEARTBEAT_SECONDS = 10
OVERDUE_BACKLOG_SECONDS = 60
RECENT_GAP_SECONDS = 300


def _heartbeat_path(spool):
    return spool.with_name(f"{spool.name}.ingest-health")


def _write_heartbeat(spool):
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", dir=spool.parent, prefix=".ingest-health-", delete=False
        ) as temp:
            temporary_path = Path(temp.name)
            temp.write(datetime.now(UTC).isoformat())
        temporary_path.replace(_heartbeat_path(spool))
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except TypeError, ValueError:
        return None


def _read_ingest_heartbeat(spool):
    try:
        with _heartbeat_path(spool).open() as marker:
            return _timestamp(marker.read(80))
    except OSError:
        return None


def _ingest_batch(spool, writer):
    batch = read_batch(str(spool), BATCH_SIZE)
    if batch:
        writer.write(batch)
        acknowledge(str(spool), [event["event_id"] for event in batch])
    return len(batch)


class Command(BaseCommand):
    help = "Manage the private OpenPandect request activity store."
    # This command must run without a connection to the case database.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("init", "ingest", "check"))
        parser.add_argument(
            "--once", action="store_true", help="Ingest at most one batch and exit."
        )

    def handle(self, *args, **options):
        action = options["action"]
        if options["once"] and action != "ingest":
            raise CommandError("--once is only available with ingest.")
        if action == "init":
            try:
                initialize_schema()
            except StoreUnavailable as error:
                raise CommandError(str(error)) from None
            self.stdout.write("Request activity schema is ready.")
            return

        path = os.environ.get("PANDECT_REQUEST_ACTIVITY_SPOOL", "")
        if not path:
            raise CommandError(
                "Set PANDECT_REQUEST_ACTIVITY_SPOOL to the private buffer file."
            )
        spool = Path(path)
        if action == "check":
            self._report_health(spool)
        else:
            self._ingest(spool, once=options["once"])

    def _ingest(self, spool, *, once):
        try:
            writer = Writer()
        except StoreUnavailable as error:
            raise CommandError(str(error)) from None
        stop = Event()
        old_handler = signal.signal(signal.SIGTERM, lambda *_: stop.set())
        next_heartbeat = 0
        delay = POLL_SECONDS
        failing = False
        heartbeat_unavailable = False
        try:
            while not stop.is_set():
                try:
                    if monotonic() >= next_heartbeat:
                        writer.verify()
                        try:
                            _write_heartbeat(spool)
                        except OSError:
                            if not heartbeat_unavailable:
                                self.stderr.write(
                                    "Request activity ingest heartbeat unavailable."
                                )
                            heartbeat_unavailable = True
                        else:
                            heartbeat_unavailable = False
                        next_heartbeat = monotonic() + HEARTBEAT_SECONDS
                    count = _ingest_batch(spool, writer)
                except OSError, sqlite3.Error, StoreUnavailable:
                    if once:
                        raise CommandError(
                            "Ingest failed. The buffer can be replayed."
                        ) from None
                    if not failing:
                        self.stderr.write(
                            "Request activity ingest is degraded; retrying."
                        )
                        failing = True
                    stop.wait(delay)
                    delay = min(delay * 2, HEARTBEAT_SECONDS)
                    continue
                if once:
                    self.stdout.write(f"Ingested {count} buffered request records.")
                    return
                if failing:
                    self.stdout.write("Request activity ingest recovered.")
                    failing = False
                delay = POLL_SECONDS
                if not count:
                    stop.wait(POLL_SECONDS)
        finally:
            try:
                writer.close()
            finally:
                signal.signal(signal.SIGTERM, old_handler)

    def _report_health(self, spool):
        try:
            capture = read_health(str(spool)) or {}
        except OSError, sqlite3.Error:
            capture = {}
        now = datetime.now(UTC)
        capture_heartbeat = _timestamp(capture.get("heartbeat_at"))
        ingest_heartbeat = _read_ingest_heartbeat(spool)
        gap = _timestamp(capture.get("last_gap_at"))
        oldest = _timestamp(capture.get("queued_oldest_at"))
        queued = capture.get("queued_count")
        problems = []
        for component, heartbeat in (
            ("capture", capture_heartbeat),
            ("ingest", ingest_heartbeat),
        ):
            if (
                heartbeat is None
                or (now - heartbeat).total_seconds() > STALE_HEARTBEAT_SECONDS
            ):
                problems.append(f"{component} heartbeat is absent or stale")
        if gap and (now - gap).total_seconds() <= RECENT_GAP_SECONDS:
            problems.append("a capture gap occurred recently")
        if oldest and (now - oldest).total_seconds() > OVERDUE_BACKLOG_SECONDS:
            problems.append("the request buffer is overdue")
        buffered = queued if isinstance(queued, int) else "unknown"
        self.stdout.write(f"Request activity buffered records: {buffered}.")
        if problems:
            raise CommandError("; ".join(problems))
        self.stdout.write("Request activity capture and ingest are current.")

"""Exercise replay, filtering, and summaries with synthetic request activity."""

import os
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
from django.core.management.base import CommandError
from psycopg.conninfo import conninfo_to_dict

from request_activity.management.commands.request_activity import (
    Command,
    _ingest_batch,
    _write_heartbeat,
)
from request_activity.spool import open_writer, read_batch, write_batch
from request_activity.store import (
    Reader,
    StoreUnavailable,
    VersionCounts,
    Writer,
    initialize_schema,
    normalize_fingerprint,
    normalize_ip,
)

TEST_DSN = os.environ.get("PANDECT_REQUEST_ACTIVITY_TEST_DSN", "")


class RequestStoreTests(TestCase):
    def test_exact_values_are_canonical(self):
        self.assertEqual(normalize_ip(" 2001:0db8:0::123 "), "2001:db8::123")
        self.assertEqual(normalize_ip("::ffff:192.0.2.1"), "192.0.2.1")
        self.assertEqual(normalize_fingerprint(f"V2:{'A' * 64}"), f"v2:{'a' * 64}")
        for value in ("2001:db8::1%en0", "192.0.2.1/24", "192.0.2.1, 192.0.2.2"):
            with self.assertRaises(ValueError):
                normalize_ip(value)
        with self.assertRaises(ValueError):
            normalize_fingerprint("v1:ab")

    @patch.dict(
        os.environ,
        {"PANDECT_REQUEST_ACTIVITY_READER_DSN": "dbname=example user=reader"},
    )
    def test_reader_cannot_use_the_application_database(self):
        with (
            patch.dict(os.environ, {"POSTGRES_DB": "example"}),
            self.assertRaises(StoreUnavailable),
        ):
            with Reader():
                self.fail("Reject the Pandect database before connecting.")

    @patch("request_activity.management.commands.request_activity.acknowledge")
    @patch("request_activity.management.commands.request_activity.read_batch")
    def test_ingest_acknowledges_only_after_commit(self, read_batch, acknowledge):
        spool = Path("synthetic-buffer.sqlite3")
        event = {"event_id": str(uuid4())}
        read_batch.return_value = [event]
        writer = Mock()
        writer.write.side_effect = StoreUnavailable
        with self.assertRaises(StoreUnavailable):
            _ingest_batch(spool, writer)
        acknowledge.assert_not_called()

        writer.write.side_effect = lambda batch: acknowledge.assert_not_called()
        self.assertEqual(_ingest_batch(spool, writer), 1)
        acknowledge.assert_called_once_with(str(spool), [event["event_id"]])

    def test_health_tracks_both_processes_without_visitor_traffic(self):
        with TemporaryDirectory() as temporary_dir:
            spool = Path(temporary_dir) / "spool.sqlite3"
            now = datetime.now(UTC)
            with open_writer(spool) as buffered:
                write_batch(buffered, [], now.isoformat(), None)

            output = StringIO()
            command = Command(stdout=output)
            with self.assertRaisesRegex(CommandError, "ingest heartbeat"):
                command._report_health(spool)

            _write_heartbeat(spool)
            command._report_health(spool)
            self.assertIn("buffered records: 0", output.getvalue())

            with open_writer(spool) as buffered:
                gap = now - timedelta(seconds=1)
                write_batch(buffered, [], now.isoformat(), gap.isoformat())
            with self.assertRaisesRegex(CommandError, "capture gap occurred recently"):
                command._report_health(spool)

    @patch("request_activity.management.commands.request_activity.Writer")
    @patch("request_activity.management.commands.request_activity._ingest_batch")
    @patch(
        "request_activity.management.commands.request_activity._write_heartbeat",
        side_effect=OSError,
    )
    def test_heartbeat_write_failure_does_not_block_ingest(
        self, heartbeat, ingest, writer
    ):
        spool = Path("synthetic-buffer.sqlite3")
        ingest.return_value = 1
        output = StringIO()
        command = Command(stdout=output, stderr=StringIO())
        command._ingest(spool, once=True)
        heartbeat.assert_called_once_with(spool)
        ingest.assert_called_once_with(spool, writer.return_value)
        self.assertIn("Ingested 1 buffered request records", output.getvalue())


@skipUnless(
    TEST_DSN,
    "Requires a dedicated disposable request activity PostgreSQL test database.",
)
class RequestStorePostgreSQLTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        database = conninfo_to_dict(TEST_DSN).get("dbname", "")
        if not database.startswith(("test_", "synthetic-")):
            raise AssertionError(
                "Use an explicitly named disposable request activity test database."
            )
        with psycopg.connect(TEST_DSN) as connection:
            if (
                connection.execute(
                    "SELECT to_regnamespace('request_activity')"
                ).fetchone()[0]
                is not None
            ):
                raise AssertionError("The disposable telemetry schema must be empty.")
        cls.enterClassContext(
            patch.dict(
                os.environ,
                {
                    "PANDECT_REQUEST_ACTIVITY_MIGRATION_DSN": TEST_DSN,
                    "PANDECT_REQUEST_ACTIVITY_WRITER_DSN": TEST_DSN,
                    "PANDECT_REQUEST_ACTIVITY_READER_DSN": TEST_DSN,
                },
            )
        )
        cls.addClassCleanup(cls.drop_schema)
        initialize_schema()
        initialize_schema()

    @classmethod
    def drop_schema(cls):
        with psycopg.connect(TEST_DSN) as connection:
            connection.execute("DROP SCHEMA IF EXISTS request_activity CASCADE")

    def test_replay_lookup_keyset_and_repeat_versions(self):
        first = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
        second = datetime(2026, 1, 2, 0, 1, tzinfo=UTC)
        original = f"v1:{'a' * 64}"
        second_version = f"v2:{'a' * 64}"

        def event(when, fingerprint, *, public_html=True):
            return {
                "event_id": str(uuid4()),
                "occurred_at": when.isoformat(),
                "method": "GET",
                "status": 200,
                "route": "home" if public_html else "cases",
                "ip": "2001:db8::1",
                "fingerprint": fingerprint,
                "public_html": public_html,
            }

        batch = [
            event(first, original),
            event(first, original),
            event(second, original),
            event(second, second_version),
            event(second, None),
            event(first, second_version, public_html=False),
        ]
        writer = Writer()
        self.addCleanup(writer.close)
        writer.verify()
        with TemporaryDirectory() as temporary_dir:
            spool = Path(temporary_dir) / "spool.sqlite3"
            with open_writer(spool) as buffered:
                write_batch(buffered, batch, second.isoformat(), None)
            writer.write(batch)
            self.assertEqual(_ingest_batch(spool, writer), len(batch))
            self.assertEqual(read_batch(spool, limit=10), [])

        with Reader() as reader:
            first_page = reader.lookup(fingerprint=original, limit=2)
            second_page = reader.lookup(
                fingerprint=original, cursor=first_page.next_cursor, limit=2
            )
            self.assertEqual(
                [row.occurred_at for row in first_page.rows], [second, first]
            )
            self.assertEqual(first_page.first_seen, first)
            self.assertEqual(first_page.last_seen, second)
            self.assertEqual(len(second_page.rows), 1)
            self.assertIsNone(second_page.next_cursor)
            self.assertNotIn(
                second_page.rows[0].event_id, [row.event_id for row in first_page.rows]
            )
            self.assertEqual(
                len(reader.lookup(ip="2001:0db8::1", before=second).rows), 3
            )
            report = reader.repeat_summary(end_date=date(2026, 1, 2))

        self.assertEqual(report.end_date - report.start_date, timedelta(days=29))
        self.assertEqual(report.eligible_requests, 5)
        self.assertEqual(report.unknown_fingerprints, 1)
        self.assertEqual(
            report.versions, (VersionCounts("v1", 1, 1), VersionCounts("v2", 1, 0))
        )
        self.assertEqual(report.latest_record_at, second)

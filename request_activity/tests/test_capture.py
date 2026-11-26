import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from ipaddress import ip_network
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from gunicorn.asgi.parser import CallbackRequest, PythonProtocol
from gunicorn.asgi.protocol import ASGIProtocol, ASGIResponseInfo
from gunicorn.config import Config

from config.privacy import PrivateAccessLogger
from request_activity import spool
from request_activity.capture import QUEUE_SIZE, RequestCapture

NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)
KEY = b"synthetic-32-byte-key-for-tests-only"
TRUSTED_PEER = "198.51.100.5"
FORWARDED_IP = "2001:db8::7"
BROWSER_HEADERS = (
    ("HOST", "openpandect.test"),
    ("USER-AGENT", " Synthetic   Browser "),
    ("ACCEPT-LANGUAGE", " en-CA "),
)


class RequestCaptureTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "private.sqlite3"
        self.logger = Mock()
        self.capture = RequestCapture(
            hosts={"openpandect.test"},
            spool_path=self.path,
            trusted_proxies=(ip_network("198.51.100.0/24"),),
            key=KEY,
            version=1,
            logger=self.logger,
            now=lambda: NOW,
        )

    def enqueue(self, *, path="/", headers=(), peer=TRUSTED_PEER, status=200):
        self.capture.enqueue(
            SimpleNamespace(status=status, headers=[("content-type", "text/html")]),
            SimpleNamespace(headers=[*BROWSER_HEADERS, *headers]),
            {
                "REMOTE_ADDR": peer,
                "PATH_INFO": path,
                "RAW_URI": f"{path}?q=synthetic-query",
                "REQUEST_METHOD": "GET",
            },
            timedelta(seconds=2),
        )

    def take_event(self):
        return self.capture.queue.get_nowait().event(KEY, 1)

    def test_origin_ip_and_fingerprint_are_independent(self):
        self.enqueue(headers=[("CF-CONNECTING-IP", FORWARDED_IP)])
        trusted = self.take_event()
        self.enqueue(headers=[("CF-CONNECTING-IP", FORWARDED_IP)], peer="192.0.2.45")
        untrusted = self.take_event()
        self.enqueue(
            headers=[
                ("CF-CONNECTING-IP", FORWARDED_IP),
                ("Cf-Connecting-Ip", FORWARDED_IP),
            ]
        )
        ambiguous = self.take_event()

        self.assertEqual(trusted["ip"], FORWARDED_IP)
        self.assertIsNone(untrusted["ip"])
        self.assertIsNone(ambiguous["ip"])
        self.assertRegex(trusted["fingerprint"], r"^v1:[0-9a-f]{64}$")
        self.assertEqual(trusted["fingerprint"], untrusted["fingerprint"])
        self.assertEqual(
            trusted["occurred_at"], (NOW - timedelta(seconds=2)).isoformat()
        )
        self.assertTrue(trusted["public_html"])

    def test_routes_and_host_are_allowlisted(self):
        self.enqueue(
            path="/cases/synthetic-case-id/evidence/synthetic-evidence-id/",
            headers=[("COOKIE", "synthetic-cookie"), ("REFERER", "synthetic-referer")],
        )
        event = self.take_event()
        self.assertEqual(event["route"], "cases")
        self.assertFalse(event["public_html"])
        for value in (
            "case-id",
            "evidence-id",
            "query",
            "cookie",
            "referer",
            "Browser",
        ):
            self.assertNotIn(value, str(event))
        self.enqueue(status=404)
        self.assertFalse(self.take_event()["public_html"])
        self.enqueue(headers=[("Host", "openpandect.test")])
        self.assertTrue(self.capture.queue.empty())
        self.enqueue(headers=[("User-Agent", "synthetic-duplicate")])
        self.assertIsNone(self.take_event()["fingerprint"])

    def test_original_gunicorn_header_pairs_retain_duplicate_forwarded_ip(self):
        parser = PythonProtocol()
        parser.feed(
            b"GET /?q=synthetic-query HTTP/1.1\r\nHost: openpandect.test\r\n"
            b"CF-Connecting-IP: 2001:db8::1\r\nCF-Connecting-IP: 2001:db8::2\r\n\r\n"
        )
        request = CallbackRequest.from_parser(parser)
        environ = ASGIProtocol._build_environ(
            None, request, None, (TRUSTED_PEER, 12345)
        )
        response = ASGIResponseInfo(200, [(b"content-type", b"text/html")], 0)
        self.capture.enqueue(response, request, environ, timedelta())
        self.assertEqual(environ["HTTP_CF_CONNECTING_IP"], "2001:db8::2")
        self.assertIsNone(self.take_event()["ip"])

    def test_configured_origin_accepts_equivalent_host_authorities(self):
        for origin, authority in (
            ("https://openpandect.test", "openpandect.test.:443"),
            ("http://[::1]:8000", "[::1]:8000"),
        ):
            with (
                self.subTest(origin=origin),
                patch.dict(
                    os.environ,
                    {
                        "PANDECT_PUBLIC_ORIGIN": origin,
                        "PANDECT_REQUEST_ACTIVITY_SPOOL": str(self.path),
                        "PANDECT_REQUEST_ACTIVITY_KEY": KEY.decode(),
                    },
                    clear=True,
                ),
            ):
                capture = RequestCapture.from_environment(self.logger)
                self.assertIn(authority, capture.hosts)

    def test_full_memory_buffer_drops_without_using_spool_or_logger(self):
        with patch.object(spool, "open_writer") as writer:
            for _index in range(QUEUE_SIZE + 1):
                self.enqueue()
        writer.assert_not_called()
        self.logger.assert_not_called()
        self.assertEqual(self.capture.queue.qsize(), QUEUE_SIZE)
        self.assertEqual(self.capture.dropped, 1)
        self.assertEqual(self.capture.last_gap_at, NOW.isoformat())

    def test_shutdown_drains_pending_events_into_private_spool(self):
        self.enqueue(headers=[("CF-CONNECTING-IP", FORWARDED_IP)])
        self.capture.last_gap_at = NOW.isoformat()
        self.capture.start()
        self.capture.stop()
        events = spool.read_batch(self.path, 10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ip"], FORWARDED_IP)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(
            spool.read_health(self.path),
            {
                "heartbeat_at": NOW,
                "last_gap_at": NOW,
                "queued_oldest_at": NOW,
                "queued_count": 1,
            },
        )
        self.assertEqual(spool.read_batch(self.path, 10), events)
        spool.acknowledge(self.path, [events[0]["event_id"]])
        self.assertEqual(spool.read_batch(self.path, 10), [])
        self.assertEqual(spool.read_health(self.path)["queued_count"], 0)

    def test_sqlite_capacity_preserves_committed_events(self):
        self.enqueue()
        event = self.take_event()
        self.enqueue()
        too_large = self.take_event()
        too_large["route"] = "synthetic" * 100_000
        with (
            patch.object(spool, "MAX_BYTES", 64 * 1024),
            spool.open_writer(self.path) as db,
        ):
            spool.write_batch(db, [event], NOW.isoformat(), None)
            with self.assertRaises(sqlite3.OperationalError):
                spool.write_batch(db, [too_large], NOW.isoformat(), None)
            self.assertEqual(spool.read_batch(self.path, 10), [event])
            spool.acknowledge(self.path, [event["event_id"]])
            self.assertEqual(spool.read_batch(self.path, 10), [])

    def test_overlapping_writers_preserve_both_events_and_latest_gap(self):
        self.enqueue()
        first_event = self.take_event()
        self.enqueue()
        second_event = self.take_event()
        later = (NOW + timedelta(seconds=1)).isoformat()
        earlier = NOW.isoformat()
        with (
            spool.open_writer(self.path) as first_writer,
            spool.open_writer(self.path) as second_writer,
        ):
            spool.write_batch(second_writer, [second_event], later, later)
            spool.write_batch(first_writer, [first_event], earlier, earlier)
        self.assertEqual(spool.read_batch(self.path, 10), [second_event, first_event])
        self.assertEqual(
            spool.read_health(self.path)["last_gap_at"], NOW + timedelta(seconds=1)
        )
        self.assertEqual(
            spool.read_health(self.path)["heartbeat_at"], NOW + timedelta(seconds=1)
        )


class RequestAccessLoggerTests(SimpleTestCase):
    def make_logger(self, enabled):
        with (
            patch("config.privacy.capture_enabled", return_value=enabled),
            patch("gunicorn.glogging.logging.getLogger", side_effect=logging.Logger),
        ):
            return PrivateAccessLogger(Config())

    def test_disabled_uses_existing_access_logger(self):
        logger = self.make_logger(False)
        with patch("gunicorn.glogging.Logger.access") as access:
            logger.access(None, None, {}, timedelta())
        access.assert_called_once_with(None, None, {}, timedelta())

    def test_enabled_skips_synchronous_access_logger(self):
        logger = self.make_logger(True)
        capture = Mock()
        logger._request_capture = capture
        with patch("gunicorn.glogging.Logger.access") as access:
            logger.access(None, None, {}, timedelta())
        capture.enqueue.assert_called_once_with(None, None, {}, timedelta())
        access.assert_not_called()

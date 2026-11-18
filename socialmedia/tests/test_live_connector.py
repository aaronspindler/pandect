"""Exercise the loopback broker with an inert Chromium driver and synthetic handles."""

import os
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from threading import Event, Thread
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from instagram_pilot import live_host
from socialmedia import instagram_connector as connector


class LiveConnectorTests(SimpleTestCase):
    def setUp(self):
        self.user = str(uuid4())
        self.target = "fixture.target"
        self.observer = "fixture.observer"
        self.driver = Mock()
        self.browser = live_host.LiveBrowser(
            self.driver, self.user, [self.target], self.observer
        )
        self.browser.clock = Mock()
        self.visible = self.enterContext(
            patch.object(live_host, "visible_observer", return_value=self.observer)
        )
        self.collect = self.enterContext(patch.object(live_host, "collect_rosters"))
        self.result = {
            "source": "instagram",
            "collector_version": "instagram-endpoints-v1",
            "target": self.target,
            "observer": self.observer,
            "followers": {"members": [], "complete": True, "reason": ""},
            "following": {"members": [], "complete": True, "reason": ""},
        }
        self.collect.return_value = self.result
        self.server = live_host.BrowserServer(("127.0.0.1", 0), live_host.LiveHandler)
        self.config = {
            "url": f"http://127.0.0.1:{self.server.server_port}",
            "token": "fixture-broker-test-only-credential",
            "user_id": self.user,
            "case_id": str(uuid4()),
            "targets": [self.target],
            "observer": self.observer,
            "min_hours": 6,
        }
        self.server.host_browser = self.browser
        self.server.auth = ("Bearer " + self.config["token"]).encode()
        self.addCleanup(self.server.server_close)
        thread = Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.enterContext(self.settings(PANDECT_INSTAGRAM_LIVE=self.config))

    def pair(self):
        pending = connector.start_pairing(self.user)["id"]
        return connector.finish_pairing(self.user, pending)["session"]

    def test_waiting_for_confirmation_dispatches_browser_events_without_reading_dom(
        self,
    ):
        dispatched = Event()
        context = self.driver.chromium.launch.return_value.new_context.return_value
        page = context.new_page.return_value
        page.wait_for_timeout.side_effect = lambda unused: dispatched.set()
        pending = connector.start_pairing(self.user)["id"]
        self.assertTrue(dispatched.wait(2))
        self.visible.assert_not_called()
        self.collect.assert_not_called()
        connector.cancel_pairing(self.user, pending)

    def test_idle_client_times_out_before_headers_and_next_connection_can_pair(self):
        with patch.object(live_host.LiveHandler, "timeout", 0.1):
            idle = HTTPConnection(*self.server.server_address, timeout=2)
            try:
                idle.connect()
                self.assertEqual(idle.sock.recv(1), b"")
            finally:
                idle.close()
        self.assertTrue(self.pair())

    def test_pair_pending_then_finished_collects_only_the_configured_account(self):
        with self.assertRaisesMessage(connector.ConnectorError, "access_lost"):
            connector.start_pairing(str(uuid4()))
        self.driver.chromium.launch.assert_not_called()
        self.visible.return_value = ""
        with patch.dict(os.environ, {"PANDECT_TEST_BROWSER_SECRET": "fixture-only"}):
            pending = connector.start_pairing(self.user)["id"]
        self.driver.chromium.launch.assert_called_once()
        launch = self.driver.chromium.launch.call_args.kwargs
        self.assertIs(launch["headless"], False)
        self.assertIs(launch["chromium_sandbox"], True)
        self.assertFalse("PANDECT_TEST_BROWSER_SECRET" in launch["env"])
        with self.assertRaisesMessage(connector.ConnectorError, "pairing_pending"):
            connector.finish_pairing(self.user, pending)
        with self.assertRaisesMessage(connector.ConnectorError, "access_lost"):
            connector.finish_pairing(str(uuid4()), pending)
        self.visible.return_value = self.observer
        connection = connector.finish_pairing(self.user, pending)
        self.assertEqual(
            (connection["observer"], connection["source"]), (self.observer, "instagram")
        )
        with self.assertRaisesMessage(connector.ConnectorError, "pairing_expired"):
            connector.finish_pairing(self.user, pending)
        with self.assertRaisesMessage(connector.ConnectorError, "access_lost"):
            connector.collect(connection["session"], "out.of.scope")
        self.collect.assert_not_called()
        self.assertEqual(
            connector.collect(connection["session"], self.target), self.result
        )
        self.collect.assert_called_once()
        self.assertEqual(self.collect.call_args.args[1:], (self.target, self.observer))
        connector.revoke_live(connection["session"])
        connector.revoke_live(connection["session"])
        with self.assertRaisesMessage(connector.ConnectorError, "disconnected"):
            connector.collect(connection["session"], self.target)
        self.assertIsNone(self.browser.browser)

    def test_cancel_expiry_and_unexpected_observer_retire_the_browser(self):
        pending = connector.start_pairing(self.user)["id"]
        connector.cancel_pairing(self.user, pending)
        self.assertIsNone(self.browser.browser)
        with self.assertRaisesMessage(connector.ConnectorError, "pairing_expired"):
            connector.finish_pairing(self.user, pending)
        pending = connector.start_pairing(self.user)["id"]
        self.browser.expires = 0
        with self.assertRaisesMessage(connector.ConnectorError, "pairing_expired"):
            connector.finish_pairing(self.user, pending)
        pending = connector.start_pairing(self.user)["id"]
        self.visible.return_value = "different.observer"
        with self.assertRaisesMessage(connector.ConnectorError, "access_lost"):
            connector.finish_pairing(self.user, pending)
        self.assertIsNone(self.browser.browser)

    def test_case_scope_minimum_interval_and_session_expiration(self):
        self.assertTrue(connector.permits(self.user, self.config["case_id"]))
        self.assertFalse(connector.permits(self.user, str(uuid4())))
        self.assertTrue(connector.allowed_target("instagram", self.target, 6))
        self.assertFalse(connector.allowed_target("instagram", self.target, 1))
        self.assertFalse(connector.allowed_target("synthetic", self.target, 6))
        session = self.pair()
        with self.assertRaisesMessage(live_host.LiveBrowserError, "access_lost"):
            self.browser.operation(
                "/collect",
                {"session": self.browser.session, "target": "outside.fixture"},
            )
        self.browser.expires = 0
        with self.assertRaisesMessage(connector.ConnectorError, "disconnected"):
            connector.collect(session, self.target)
        self.collect.assert_not_called()
        self.assertIsNone(self.browser.browser)

    def test_scope_rotation_or_swapped_token_purpose_blocks_outbound_requests(self):
        session = self.pair()
        pending = connector.start_pairing(self.user)["id"]
        with patch.object(connector, "_live_request") as transport:
            with self.assertRaises(connector.ConnectorError):
                connector.collect(pending, self.target)
            for replacement in (
                {"url": "http://127.0.0.1:1"},
                {"user_id": str(uuid4())},
                {"case_id": str(uuid4())},
                {"token": "replacement-test-only-credential"},
            ):
                with (
                    self.subTest(replacement=list(replacement)),
                    self.settings(PANDECT_INSTAGRAM_LIVE=self.config | replacement),
                    self.assertRaises(connector.ConnectorError),
                ):
                    connector.revoke_live(session)
            transport.assert_not_called()

    def test_daily_check_reuses_connection_until_fixed_two_day_expiry(self):
        confirmed_at = 100.0
        with patch.object(live_host, "monotonic", return_value=confirmed_at) as clock:
            session = self.pair()
            self.assertEqual(connector.collect(session, self.target), self.result)
            self.assertEqual(
                self.collect.call_args.kwargs["deadline"], confirmed_at + 600
            )
            clock.return_value = confirmed_at + 24 * 3600
            self.assertEqual(connector.collect(session, self.target), self.result)
            clock.return_value = confirmed_at + 48 * 3600
            with self.assertRaisesMessage(connector.ConnectorError, "disconnected"):
                connector.collect(session, self.target)
        self.driver.chromium.launch.assert_called_once()
        self.assertEqual(self.collect.call_count, 2)

    def test_host_errors_cooldown_and_untrusted_responses_are_sanitized(self):
        session = self.pair()
        self.collect.side_effect = live_host.LiveBrowserError("cooldown")
        with self.assertRaisesMessage(connector.ConnectorError, "cooldown"):
            connector.collect(session, self.target)
        self.collect.side_effect = live_host.BrowserError(
            "test-only private browser detail"
        )
        with self.assertRaisesMessage(connector.ConnectorError, "unavailable") as error:
            connector.collect(session, self.target)
        self.assertNotIn("private browser detail", str(error.exception))
        self.collect.side_effect = RuntimeError("private fixture detail")
        with self.assertRaisesMessage(connector.ConnectorError, "unavailable"):
            connector.collect(session, self.target)
        self.collect.side_effect = None
        for changed in (
            {"target": "other.target"},
            {"observer": "other.observer"},
            {"source": "synthetic"},
            {"collector_version": "instagram-browser-v1"},
            {"collector_version": "unknown"},
        ):
            self.collect.return_value = self.result | changed
            with (
                self.subTest(changed=changed),
                self.assertRaisesMessage(connector.ConnectorError, "invalid_response"),
            ):
                connector.collect(session, self.target)
        self.collect.return_value = self.result
        with (
            patch.object(connector, "MAX_RESPONSE", 50),
            self.assertRaisesMessage(connector.ConnectorError, "invalid_response"),
        ):
            connector.collect(session, self.target)

    def test_http_origin_authorization_and_request_bounds(self):
        url_host = self.server.server_address
        common = {
            "Authorization": self.server.auth.decode(),
            "Content-Type": "application/json",
        }
        for headers, body, expected in (
            ({"Authorization": "Bearer wrong"}, b"{}", 403),
            ({"Origin": "https://example.test"}, b"{}", 403),
            ({"Content-Type": "text/plain"}, b"{}", 400),
            ({}, b"x" * 2050, 400),
            ({}, b"[]", 400),
        ):
            with self.subTest(expected=expected, headers=list(headers)):
                http = HTTPConnection(*url_host, timeout=3)
                try:
                    http.request("POST", "/pair/start", body, common | headers)
                    response = http.getresponse()
                    self.assertEqual(response.status, expected)
                    self.assertNotIn(self.config["token"].encode(), response.read())
                finally:
                    http.close()
        self.driver.chromium.launch.assert_not_called()

    def test_redirect_is_not_followed_and_cooldown_preserves_retry_after(self):
        paths = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/redirect-destination")
                self.end_headers()

            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server.RequestHandlerClass = RedirectHandler
        with self.assertRaisesMessage(connector.ConnectorError, "unavailable"):
            connector.start_pairing(self.user)
        self.assertEqual(paths, ["/pair/start"])

        class CooldownHandler(RedirectHandler):
            def do_POST(self):
                self.send_response(429)
                self.send_header("Retry-After", "3600")
                self.end_headers()

        self.server.RequestHandlerClass = CooldownHandler
        with self.assertRaisesMessage(connector.ConnectorError, "cooldown") as error:
            connector.start_pairing(self.user)
        self.assertEqual(error.exception.retry_after, 3600)


class LiveHostActionTests(SimpleTestCase):
    def test_actions_respect_spacing_and_deadline_across_calls(self):
        now = 100.0

        def wait(seconds):
            nonlocal now
            now += seconds

        clock = live_host.ActionClock(now=lambda: now, wait=wait)
        self.assertTrue(clock.before(110))
        self.assertEqual(now, 100)
        self.assertFalse(clock.before(105))
        self.assertTrue(clock.before(106))
        self.assertEqual(now, 105)
        now = 115
        self.assertTrue(clock.before(116))
        self.assertEqual(now, 115)

    def test_browser_allows_only_official_navigation_and_assets(self):
        for url, navigation, allowed in (
            ("https://www.instagram.com/accounts/login/", True, True),
            ("https://images.cdninstagram.com/img", False, True),
            ("https://images.cdninstagram.com/page", True, False),
            ("https://instagram.com.example.test/a", False, False),
            ("http://www.instagram.com/a", True, False),
            ("https://name@www.instagram.com/a", True, False),
            ("https://www.instagram.com:8000/a", True, False),
        ):
            with self.subTest(url=url, navigation=navigation):
                route = Mock()
                route.request.url = url
                route.request.is_navigation_request.return_value = navigation
                live_host.restrict_destinations(route)
                self.assertEqual(route.continue_.call_count, int(allowed))
                self.assertEqual(route.abort.call_count, int(not allowed))

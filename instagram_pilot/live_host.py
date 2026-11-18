"""Single-observer, loopback-only browser for the local Instagram qualification."""

import hmac
import json
import logging
import os
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import SHUT_RD
from threading import Timer
from time import monotonic, sleep
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright

from instagram_pilot.endpoint_roster import collect_rosters
from instagram_pilot.live_roster import LiveBrowserError, visible_observer
from socialmedia.instagram_connector import LIVE_COLLECTION_SECONDS

LOGIN = "https://www.instagram.com/accounts/login/"
MIN_ACTION_SECONDS = 5
PAIR_SECONDS = 600
SESSION_SECONDS = 48 * 3600


class ActionClock:
    def __init__(self, *, now=monotonic, wait=sleep):
        self.now = now
        self.wait = wait
        self.next_at = 0.0

    def before(self, deadline):
        when = max(self.now(), self.next_at)
        if when >= deadline:
            return False
        self.wait(max(0, when - self.now()))
        if self.now() >= deadline:
            return False
        self.next_at = self.now() + MIN_ACTION_SECONDS
        return True


def restrict_destinations(route):
    url = urlsplit(route.request.url)
    # Credentials are entered in Instagram's top-level web page. The browser
    # owns its HTTPS traffic. No password, network body or cookie is inspected.
    host = url.hostname or ""
    domains = ("instagram.com", "cdninstagram.com", "fbcdn.net")
    if route.request.is_navigation_request():
        domains = ("instagram.com",)
    if (
        url.scheme == "https"
        and url.port in {443, None}
        and url.username is None
        and url.password is None
        and any(host == d or host.endswith("." + d) for d in domains)
    ):
        route.continue_()
    else:
        route.abort()


class LiveBrowser:
    """All Playwright operations run on the one local service thread."""

    def __init__(self, driver, user_id, targets, observer=""):
        self.driver = driver
        self.user_id = user_id
        self.targets = set(targets)
        self.expected = observer
        self.clock = ActionClock()
        self.browser = None
        self.page = None
        self.pair = self.session = self.observer = ""
        self.expires = 0.0

    def close(self):
        if self.browser:
            try:
                self.browser.close()
            except BrowserError:
                pass
        self.browser = self.page = None
        self.pair = self.session = self.observer = ""
        self.expires = 0.0

    def expire(self):
        if self.browser and (
            monotonic() >= self.expires or not self.browser.is_connected()
        ):
            self.close()

    def _owns(self, data):
        if data.get("user_id") != self.user_id:
            raise LiveBrowserError("access_lost")

    def operation(self, path, data):
        self.expire()
        if path == "/pair/start":
            self._owns(data)
            # Starting pairing while an old connection is active is explicit in
            # the UI. Clear the old browser before any new sign-in begins.
            self.close()
            self.browser = self.driver.chromium.launch(
                headless=False,
                chromium_sandbox=True,
                env={
                    k: os.environ[k]
                    for k in (
                        "HOME",
                        "PATH",
                        "TMPDIR",
                        "LANG",
                        "DISPLAY",
                        "XAUTHORITY",
                        "XDG_RUNTIME_DIR",
                        "WAYLAND_DISPLAY",
                    )
                    if k in os.environ
                },
            )
            try:
                ctx = self.browser.new_context(
                    accept_downloads=False, service_workers="block", locale="en-US"
                )
                ctx.route("**/*", restrict_destinations)
                ctx.route_web_socket("**/*", lambda socket: socket.close())
                self.page = ctx.new_page()
                self.clock.before(monotonic() + 10)
                self.page.goto(LOGIN, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                self.close()
                raise
            self.pair = secrets.token_urlsafe(32)
            self.expires = monotonic() + PAIR_SECONDS
            return {"id": self.pair}
        if path in {"/pair/cancel", "/pair/finish"}:
            self._owns(data)
            if not self.pair or data.get("id") != self.pair:
                raise LiveBrowserError("pairing_expired")
            if path == "/pair/cancel":
                self.close()
                return {}
            observer = visible_observer(self.page)
            if not observer:
                raise LiveBrowserError("pairing_pending")
            if self.expected and observer != self.expected:
                self.close()
                raise LiveBrowserError("access_lost")
            self.observer = observer
            self.session = secrets.token_urlsafe(32)
            self.expires = monotonic() + SESSION_SECONDS
            self.pair = ""
            return {"observer": observer, "session": self.session}
        if path == "/disconnect":
            if self.session and data.get("session") == self.session:
                self.close()
            return {}
        if path == "/collect":
            if not self.session or data.get("session") != self.session:
                raise LiveBrowserError("disconnected")
            if data.get("target") not in self.targets:
                raise LiveBrowserError("access_lost")
            return collect_rosters(
                self.page,
                data["target"],
                self.observer,
                deadline=min(self.expires, monotonic() + LIVE_COLLECTION_SECONDS),
                before_load=self.clock.before,
            )
        raise LiveBrowserError("unavailable")


STATUS = {
    "access_lost": 403,
    "pairing_expired": 404,
    "challenge": 409,
    "pairing_pending": 425,
    "cooldown": 429,
    "disconnected": 401,
    "unavailable": 503,
}


class BrowserServer(HTTPServer):
    def service_actions(self):
        self.host_browser.expire()
        # Sync Playwright dispatches the browser's routing callbacks only while
        # its event loop runs. Keep sign-in responsive between HTTP requests.
        if self.host_browser.page is not None:
            try:
                self.host_browser.page.wait_for_timeout(25)
            except BrowserError:
                self.host_browser.close()

    def handle_error(self, request, client_address):
        # socketserver's default traceback can contain private request context.
        logging.getLogger(__name__).warning("Local browser request failed.")


class LiveHandler(BaseHTTPRequestHandler):
    timeout = 3

    def handle(self):
        # In addition to the per-socket timeout set by StreamRequestHandler
        # before parsing, bound the total time spent waiting for request bytes.
        deadline = self.input_deadline = Timer(self.timeout, self._end_input)
        deadline.start()
        try:
            super().handle()
        finally:
            deadline.cancel()

    def _end_input(self):
        try:
            self.connection.shutdown(SHUT_RD)
        except OSError:
            pass

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler's parse failures can reflect the request line.
        super().send_error(code)

    def log_message(self, *args):
        pass

    def do_POST(self):
        # No browser-facing interface or CORS. Reject page-originated requests.
        auth = self.headers.get("Authorization", "")
        if self.headers.get("Origin") or not hmac.compare_digest(
            auth.encode(), self.server.auth
        ):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if (
                not 0 < length <= 2048
                or self.headers.get("Content-Type") != "application/json"
            ):
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError
        except OSError, ValueError, RecursionError:
            self.send_error(400)
            return
        self.input_deadline.cancel()
        try:
            result = self.server.host_browser.operation(self.path, data)
        except LiveBrowserError as err:
            self.send_error(STATUS.get(err.code, 503))
            return
        except Exception:
            self.send_error(503)
            return
        raw = json.dumps(result, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    # Use the same validation as the application without accessing the database.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.conf import settings

    config = settings.PANDECT_INSTAGRAM_LIVE
    if not config:
        raise SystemExit("Configure the local Instagram qualification first.")
    with (
        sync_playwright() as driver,
        BrowserServer(
            ("127.0.0.1", urlsplit(config["url"]).port), LiveHandler
        ) as server,
    ):
        server.auth = ("Bearer " + config["token"]).encode()
        server.host_browser = LiveBrowser(
            driver, config["user_id"], config["targets"], config["observer"]
        )
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.host_browser.close()


if __name__ == "__main__":
    main()

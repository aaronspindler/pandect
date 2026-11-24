"""Private browser session for standalone Instagram qualification."""

import os
from time import monotonic, sleep
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError

from instagram_pilot.endpoint_roster import collect_rosters
from instagram_pilot.live_roster import LiveBrowserError, visible_observer

LOGIN = "https://www.instagram.com/accounts/login/"
MIN_ACTION_SECONDS = 5
LIVE_COLLECTION_SECONDS = 600
PAIR_SECONDS = 600


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
    """Keep all Playwright operations on the local qualification thread."""

    def __init__(self, driver, target):
        self.driver = driver
        self.target = target
        self.clock = ActionClock()
        self.browser = None
        self.page = None
        self.observer = ""
        self.expires = 0.0

    def close(self):
        if self.browser:
            try:
                self.browser.close()
            except BrowserError:
                pass
        self.browser = self.page = None
        self.observer = ""
        self.expires = 0.0

    def start_sign_in(self):
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
        self.expires = monotonic() + PAIR_SECONDS

    def confirm_sign_in(self):
        if (
            self.page is None
            or monotonic() >= self.expires
            or not self.browser.is_connected()
        ):
            raise LiveBrowserError("pairing_expired")
        observer = visible_observer(self.page)
        if not observer:
            raise LiveBrowserError("pairing_pending")
        self.observer = observer
        return observer

    def collect(self):
        if not self.observer or not self.browser.is_connected():
            raise LiveBrowserError("disconnected")
        return collect_rosters(
            self.page,
            self.target,
            self.observer,
            deadline=monotonic() + LIVE_COLLECTION_SECONDS,
            before_load=self.clock.before,
        )

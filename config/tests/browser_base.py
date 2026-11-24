"""Playwright lifecycle and helpers for explicit investigator browser suites."""

import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from threading import Condition

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.servers.basehttp import ThreadedWSGIServer
from django.db import connections
from django.test import override_settings
from django.test.testcases import LiveServerThread
from django.urls import reverse
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

from config.tests.browser_artifacts import artifact_directory


class _DrainableBrowserServer(ThreadedWSGIServer):
    def __init__(self, *args, **kwargs):
        self.connection_state = Condition()
        self.active_connections = 0
        self.draining = False
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request, client_address):
        with self.connection_state:
            if self.draining:
                self.shutdown_request(request)
                return
            self.active_connections += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.connection_state:
                self.active_connections -= 1
                self.connection_state.notify_all()

    @contextmanager
    def drained_connections(self):
        # Closing the browser can cancel fetches while the server still owns
        # their database locks. Keep new handlers out until the flush finishes.
        try:
            with self.connection_state:
                self.draining = True
                drained = self.connection_state.wait_for(
                    lambda: self.active_connections == 0, timeout=15
                )
            if not drained:
                raise TimeoutError("Browser server connections did not drain.")
            yield
        finally:
            with self.connection_state:
                self.draining = False


class DrainableBrowserLiveServerThread(LiveServerThread):
    server_class = _DrainableBrowserServer


@override_settings(
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    ACCOUNT_RATE_LIMITS=False,
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
)
class BrowserTestCase(StaticLiveServerTestCase):
    artifact_subdirectory = "browser"
    password = "synthetic-browser-correct-horse-742!"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engines = tuple(
            engine.strip()
            for engine in os.environ.get(
                "PANDECT_BROWSER_ENGINES", "chromium,webkit"
            ).split(",")
            if engine.strip()
        )
        if not cls.engines or set(cls.engines) - {"chromium", "webkit"}:
            raise ValueError(
                "PANDECT_BROWSER_ENGINES must contain chromium and/or webkit."
            )

    def setUp(self):
        super().setUp()
        # Playwright's sync API runs an event loop on the calling thread. Start
        # it only after fixtures, stop it before Django teardown, and execute
        # in-journey ORM assertions in a synchronous worker. Django's async
        # safety checks remain enabled in both the tests and application.
        self.playwright = None
        self.browsers = {}
        self.browser_context_number = 0
        self.database_worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.database_worker.shutdown)
        self.media_directory = tempfile.TemporaryDirectory(
            prefix=f"pandect-{self.artifact_subdirectory}-"
        )
        self.addCleanup(self.media_directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=self.media_directory.name))
        self.artifacts = artifact_directory(self.artifact_subdirectory)

    def make_user(self, username):
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password=self.password,
        )
        EmailAddress.objects.create(
            user=user, email=user.email, primary=True, verified=True
        )
        return user

    def url(self, name, **kwargs):
        return self.live_server_url + reverse(name, kwargs=kwargs)

    def db(self, operation, *args, **kwargs):
        def run():
            try:
                return operation(*args, **kwargs)
            finally:
                connections.close_all()

        return self.database_worker.submit(run).result()

    @contextmanager
    def browser_page(self, engine, **options):
        outermost = self.playwright is None
        if outermost:
            self.playwright = sync_playwright().start()
        context = None
        page = None
        self.browser_context_number += 1
        context_number = self.browser_context_number
        try:
            if engine not in self.browsers:
                self.browsers[engine] = getattr(self.playwright, engine).launch()
            context = self.browsers[engine].new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id=options.pop("timezone_id", "UTC"),
                color_scheme="light",
                accept_downloads=True,
                **options,
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            script_errors = []
            page.on("pageerror", lambda error: script_errors.append(str(error)))
            yield context, page
            self.assertEqual(script_errors, [], "Unexpected browser script errors")
        except Exception:
            # Preserve the failing state without masking the original assertion.
            if page is not None and not page.is_closed():
                with suppress(PlaywrightError, OSError):
                    page.screenshot(
                        path=str(
                            self.artifacts
                            / (
                                f"{engine}-failure-{self._testMethodName}"
                                f"-context-{context_number}.png"
                            )
                        ),
                        timeout=5000,
                    )
            raise
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                if outermost:
                    try:
                        for browser in self.browsers.values():
                            browser.close()
                    finally:
                        self.playwright.stop()
                        self.playwright = None
                        self.browsers = {}

    def sign_in(self, page, user=None):
        user = user or self.owner
        page.goto(self.url("account_login"))
        page.locator("input[name='login']").fill(user.email)
        page.locator("input[name='password']").fill(self.password)
        page.get_by_role("button", name=re.compile(r"^sign in$", re.I)).click()
        expect(
            page.get_by_role("heading", name="All cases", exact=True)
        ).to_be_visible()
        # The heading can render before the deferred collection script binds inputs.
        page.wait_for_load_state("domcontentloaded")

    def open_evidence_panel(self, page, name):
        """Open the record's native Info or More panel using the keyboard."""
        # Redirects can reveal Info from the URL hash during page initialization.
        page.wait_for_load_state("load")
        other_name = "more" if name == "info" else "info"
        other = page.locator(f".evidence-{other_name}")
        if other.count() and other.get_attribute("open") is not None:
            other.locator(":scope > summary").press("Enter")
        panel = page.locator(f".evidence-{name}")
        if panel.get_attribute("open") is None:
            summary = panel.locator(":scope > summary")
            summary.focus()
            summary.press("Enter")
        return panel

    def capture(self, page, engine, name):
        logo = page.locator(".wordmark picture img")
        if logo.count() and logo.is_visible():
            expected_source = logo.evaluate(
                """img => new URL(
                  matchMedia('(prefers-color-scheme: dark)').matches
                    ? img.previousElementSibling.srcset : img.src,
                  document.baseURI).href"""
            )
            expect(logo).to_have_js_property("currentSrc", expected_source)
            expect(logo).to_have_js_property("complete", True)
            expect(logo).not_to_have_js_property("naturalWidth", 0)
        page.screenshot(
            path=str(self.artifacts / f"{engine}-{name}.png"), animations="disabled"
        )

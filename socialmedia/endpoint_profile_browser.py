"""Offline Chromium checks for binding an endpoint owner to the visible profile."""

from contextlib import ExitStack
from time import monotonic
from unittest.mock import patch
from urllib.parse import urlsplit

from django.test import SimpleTestCase
from playwright.sync_api import sync_playwright

from instagram_pilot.endpoint_profile import _first_page_options, resolve_profile
from instagram_pilot.live_roster import LiveBrowserError


class EndpointProfileTests(SimpleTestCase):
    target = "controlled.test"
    observer = "reviewer.test"
    roster = "/api/v1/friendships/12345/followers/"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        driver = stack.enter_context(sync_playwright())
        cls.browser = driver.chromium.launch()
        stack.callback(cls.browser.close)

    def setUp(self):
        context = self.browser.new_context(offline=True, service_workers="block")
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.meta = ""
        self.requests = []
        self.permits = 0
        self.roster_status = 200
        self.roster_data = {"status": "ok", "users": [], "has_more": False}
        self.switch_observer = False
        self.query = "?count=12&search_surface=follow_list_page&query="
        self.app_id = "918273"
        self.decoy = ""

        def serve(route):
            url = urlsplit(route.request.url)
            if url.netloc != "www.instagram.com":
                route.abort()
                return
            self.requests.append(url.path)
            if url.path == self.roster:
                route.fulfill(status=self.roster_status, json=self.roster_data)
            elif url.path == "/":
                route.fulfill(
                    content_type="text/html",
                    body=f'<nav><a href="/{self.observer}/">Profile</a></nav>',
                )
            elif url.path == f"/{self.target}/":
                observer = (
                    "other.test"
                    if self.switch_observer and self.roster in self.requests
                    else self.observer
                )
                route.fulfill(
                    content_type="text/html",
                    body=f"""
                    <head>{self.meta}</head>
                    <nav><a href="/{observer}/" aria-label="Profile">Profile</a></nav>
                    <main><header><h2>{self.target}</h2>
                    <a href="#" onclick="{self.decoy}
                      fetch('{self.roster}{self.query}',
                            {{headers:{{'x-ig-app-id':'{self.app_id}'}}}})
                            .then(response => response.json());
                      return false">
                      1,230 followers</a><a href="#">1.2K following</a>
                    </header></main>""",
                )
            else:
                route.abort()

        context.route("**/*", serve)
        self.page.goto("https://www.instagram.com/")
        self.requests.clear()

    def resolve(self):
        def before(deadline):
            self.permits += 1
            return True

        return resolve_profile(
            self.page,
            self.target,
            self.observer,
            deadline=monotonic() + 5,
            before_load=before,
        )

    def test_verified_profile_resolves_only_public_unfiltered_request_options(self):
        self.meta = f"""
            <link rel="canonical" href="https://www.instagram.com/{self.target}/">
            <meta property="al:ios:url" content="instagram://user?id=12345">
            <meta property="instapp:owner_user_id" content="12345">"""
        self.assertEqual(
            self.resolve(),
            (
                "12345",
                {"followers": 1230, "following": None},
                {"count": "12", "query": "", "search_surface": "follow_list_page"},
                self.app_id,
                self.roster_data,
            ),
        )
        self.assertEqual(self.permits, 3)
        self.assertEqual(
            self.requests, [f"/{self.target}/", self.roster, f"/{self.target}/"]
        )

    def test_username_only_uses_website_request_then_reconfirms_observer(self):
        self.meta = f'<meta property="al:ios:url" content="instagram://user?username={self.target}">'
        self.query = ""
        self.app_id = ""
        self.decoy = f"fetch('{self.roster}?count=12&query=name');"
        self.assertEqual(
            self.resolve(),
            ("12345", {"followers": 1230, "following": None}, {}, "", self.roster_data),
        )
        self.assertEqual(self.permits, 3)
        self.assertEqual(
            self.requests,
            [f"/{self.target}/", self.roster, self.roster, f"/{self.target}/"],
        )
        self.requests.clear()
        self.switch_observer = True
        with self.assertRaisesRegex(LiveBrowserError, "challenge"):
            self.resolve()

    def test_conflicting_metadata_and_cooldown_stop_resolution(self):
        cases = [
            '<meta property="al:ios:url" content="instagram://user?username=someone.else">',
            '<meta property="instapp:owner_user_id" content="12345">'
            '<meta property="al:ios:url" content="instagram://user?id=4321">',
            '<link rel="canonical" href="https://unrelated.invalid/profile/">',
        ]
        for meta in cases:
            with self.subTest(meta=meta):
                self.meta = meta
                with self.assertRaisesRegex(LiveBrowserError, "access_lost"):
                    self.resolve()
                self.assertNotIn(self.roster, self.requests)
        self.meta = ""
        self.roster_status = 429
        with self.assertRaisesRegex(LiveBrowserError, "cooldown"):
            self.resolve()
        self.assertEqual(self.requests[-1], self.roster)

    def test_search_pagination_and_unrecognized_options_are_excluded(self):
        for query in (
            "count=101",
            "count=0",
            "query=a",
            "count=1&count=2",
            "search_surface=suggestions",
            "max_id=3",
            "session=private",
        ):
            with self.subTest(query=query):
                self.assertIsNone(_first_page_options(query))

    def test_json_refusal_in_successful_http_response_stops_before_navigation(self):
        for payload, error in (
            ({"status": "fail", "message": "challenge_required"}, "challenge"),
            ({"status": "fail", "message": "feedback_required"}, "cooldown"),
            ({"status": "ok"}, "unavailable"),
        ):
            with self.subTest(error=error):
                self.roster_data = payload
                self.requests.clear()
                with self.assertRaisesRegex(LiveBrowserError, error):
                    self.resolve()
                self.assertEqual(self.requests, [f"/{self.target}/", self.roster])

    def test_visible_refusal_prevents_initial_navigation_or_dismissing_modal(self):
        original = self.page.content()
        self.page.set_content("<h2>Try again later</h2>")
        with self.assertRaisesRegex(LiveBrowserError, "cooldown"):
            self.resolve()
        self.assertEqual(self.requests, [])
        self.page.set_content(original)
        self.decoy = (
            """document.querySelector('h2').innerText='Confirm your identity';"""
        )
        with self.assertRaisesRegex(LiveBrowserError, "challenge"):
            self.resolve()
        self.assertEqual(self.requests, [f"/{self.target}/", self.roster])

    def test_oversized_initial_response_stops_before_restoring_profile(self):
        with (
            patch("instagram_pilot.endpoint_profile.MAX_RESPONSE_BYTES", 1),
            self.assertRaisesRegex(LiveBrowserError, "unavailable"),
        ):
            self.resolve()
        self.assertEqual(self.requests, [f"/{self.target}/", self.roster])

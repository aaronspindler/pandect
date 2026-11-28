"""Exercise the signed-in roster endpoints against locally fulfilled responses."""

import json
from contextlib import ExitStack
from itertools import pairwise
from time import monotonic
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase
from playwright.sync_api import sync_playwright

from instagram_pilot.endpoint_roster import collect_rosters
from instagram_pilot.live_host import ActionClock
from instagram_pilot.live_roster import LiveBrowserError

TARGET = "profile.fixture"
OBSERVER = "observer.fixture"
OWNER_ID = "100000000001"
CURSOR = "next+page=a/b?c&d#e"
BIG_ID = 2**60 + 1


def roster_page(username, identifier, cursor=None):
    return {
        "status": "ok",
        "users": [{"username": username, "pk": identifier}],
        "has_more": cursor is not None,
        "next_max_id": cursor,
    }


class EndpointRosterBrowserTests(SimpleTestCase):
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
        self.seconds = monotonic()
        self.clock = ActionClock(now=lambda: self.seconds, wait=self.advance)
        self.actions = []
        self.requests = []
        self.responses = {
            ("followers", CURSOR): (200, roster_page("bravo", BIG_ID + 2)),
            ("following", None): (200, roster_page("charlie", "13")),
        }
        context.route("**/*", self.serve)
        self.page.goto(f"https://www.instagram.com/{TARGET}/")
        self.enterContext(
            patch(
                "instagram_pilot.endpoint_roster.resolve_profile",
                return_value=(
                    OWNER_ID,
                    {"followers": 2, "following": 1},
                    {"count": "50"},
                    "12345",
                    roster_page("alpha", BIG_ID, CURSOR),
                ),
            )
        )

    def advance(self, seconds):
        self.seconds += seconds

    def permit(self, deadline):
        if self.clock.before(deadline):
            self.actions.append(("permit", self.seconds))
            return True
        return False

    def serve(self, route):
        url = urlsplit(route.request.url)
        if url.scheme == "https" and url.netloc == "www.instagram.com":
            if url.path == f"/{TARGET}/":
                route.fulfill(
                    content_type="text/html",
                    body=f'<nav><a href="/{OBSERVER}/">Profile</a></nav>',
                )
                return
            for direction in ("followers", "following"):
                if url.path != f"/api/v1/friendships/{OWNER_ID}/{direction}/":
                    continue
                query = parse_qs(url.query)
                cursor = query.get("max_id", [None])[0]
                self.requests.append(
                    (direction, query, route.request.header_value("x-ig-app-id"))
                )
                self.actions.append(
                    ((route.request.method, direction, cursor), self.seconds)
                )
                payload = self.responses.get((direction, cursor))
                if payload:
                    status, data = payload
                    route.fulfill(
                        status=status,
                        content_type="application/json",
                        body=json.dumps(data),
                    )
                    return
        route.abort()

    def collect(self):
        return collect_rosters(
            self.page,
            TARGET,
            OBSERVER,
            deadline=monotonic() + 60,
            before_load=self.permit,
        )

    def test_paged_responses_preserve_ids_and_pace_every_request(self):
        observation = self.collect()
        self.assertEqual(observation["target_id"], OWNER_ID)
        self.assertEqual(observation["observer"], OBSERVER)
        self.assertEqual(
            observation["followers"]["members"],
            [
                {"handle": "alpha", "id": str(BIG_ID)},
                {"handle": "bravo", "id": str(BIG_ID + 2)},
            ],
        )
        self.assertEqual(
            observation["following"]["members"], [{"handle": "charlie", "id": "13"}]
        )
        for direction, pages in (("followers", 2), ("following", 1)):
            self.assertTrue(observation[direction]["complete"])
            self.assertEqual(observation[direction]["page_count"], pages)
        self.assertEqual(
            [action for action, _ in self.actions],
            [
                "permit",
                ("GET", "followers", CURSOR),
                "permit",
                ("GET", "following", None),
            ],
        )
        times = [time for action, time in self.actions if action != "permit"]
        for start, end in pairwise(times):
            self.assertGreaterEqual(end - start, 5)
        self.assertEqual(
            self.requests,
            [
                ("followers", {"count": ["50"], "max_id": [CURSOR]}, "12345"),
                ("following", {"count": ["50"]}, "12345"),
            ],
        )

    def test_access_errors_stop_before_the_other_direction(self):
        for status, code in ((429, "cooldown"), (403, "challenge")):
            with self.subTest(status=status):
                self.actions.clear()
                self.responses[("followers", CURSOR)] = (status, {"status": "fail"})
                with self.assertRaises(LiveBrowserError) as error:
                    self.collect()
                self.assertEqual(error.exception.code, code)
                self.assertEqual(
                    [action for action, _ in self.actions if isinstance(action, tuple)],
                    [("GET", "followers", CURSOR)],
                )

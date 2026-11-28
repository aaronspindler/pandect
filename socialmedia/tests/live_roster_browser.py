"""Verify the signed-in observer using locally fulfilled profile pages."""

from contextlib import ExitStack

from django.test import SimpleTestCase
from playwright.sync_api import sync_playwright

from instagram_pilot.live_roster import visible_observer

PROFILE = "qualify.example"
OBSERVER = "fixture.observer"
ORIGIN = "https://www.instagram.com"
NAV = f'<nav><a href="/{OBSERVER}/" aria-label="Profile">Profile</a></nav>'


class InstagramObserverTests(SimpleTestCase):
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
        context.route(
            f"{ORIGIN}/{PROFILE}/",
            lambda route: route.fulfill(content_type="text/html", body=NAV),
        )

    def test_visible_observer_requires_official_origin_and_unambiguous_navigation(self):
        self.page.set_content(NAV)
        self.assertEqual(visible_observer(self.page), "")

        self.page.goto(f"{ORIGIN}/{PROFILE}/")
        self.assertEqual(visible_observer(self.page), OBSERVER)
        self.page.set_content(NAV + '<input type="password">')
        self.assertEqual(visible_observer(self.page), "")
        self.page.set_content(NAV + NAV.replace(OBSERVER, "second"))
        self.assertEqual(visible_observer(self.page), "")
        self.page.set_content(
            '<nav><a aria-label="Profile" href="https://invalid.test/a/">Profile</a></nav>'
        )
        self.assertEqual(visible_observer(self.page), "")

    def test_modern_desktop_and_icon_sidebar_require_app_navigation(self):
        self.page.goto(f"{ORIGIN}/{PROFILE}/")
        sidebar = (
            '<div><a href="/">Home</a><a href="/explore/">Explore</a>'
            f'<a href="/{OBSERVER}/"><span>Profile</span></a></div>'
        )
        self.page.set_content(
            sidebar
            + '<main><article><a href="/unrelated/">Profile</a></article></main>'
        )
        self.assertEqual(visible_observer(self.page), OBSERVER)
        self.page.set_content(
            sidebar.replace(
                "<span>Profile</span>",
                f'<img alt="{OBSERVER}’s profile picture" width="20" height="20">',
            )
        )
        self.assertEqual(visible_observer(self.page), OBSERVER)
        self.page.set_content(
            f'<div role="menu"><a href="/{OBSERVER}/">'
            f'<img alt="{OBSERVER}’s profile picture" width="20" height="20"></a></div>'
        )
        self.assertEqual(visible_observer(self.page), OBSERVER)
        self.page.set_content(
            '<aside><h3>Suggested for you</h3><a href="/suggestion/">'
            '<img alt="suggestion’s profile picture" width="20" height="20">'
            "</a></aside>"
        )
        self.assertEqual(visible_observer(self.page), "")

    def test_own_profile_requires_matching_heading_and_same_origin_edit_control(self):
        self.page.goto(f"{ORIGIN}/{PROFILE}/")
        edit = '<a href="/accounts/edit/">Edit profile</a>'
        heading = f"<h2>{PROFILE}</h2>"
        self.page.set_content(f"<main><header>{heading}{edit}</header></main>")
        self.assertEqual(visible_observer(self.page), PROFILE)
        for content in (
            heading,
            edit,
            "<h2>different</h2>" + edit,
            heading
            + edit.replace("/accounts/edit/", "https://invalid.test/accounts/edit/"),
            heading + edit.replace("Edit profile", "More"),
            heading + "<article>" + edit + "</article>",
            heading + edit.replace("<a ", "<a hidden "),
            NAV + heading + edit,
        ):
            with self.subTest(content=content):
                self.page.set_content(f"<main><header>{content}</header></main>")
                self.assertEqual(visible_observer(self.page), "")

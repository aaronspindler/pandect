"""Public-page browser checks: python manage.py test config.marketing_browser."""

import os
import sys
from itertools import pairwise
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright


class MarketingBrowserTests(StaticLiveServerTestCase):
    def assert_reflow(self, page):
        self.assertTrue(
            page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1"
            ),
            f"Horizontal overflow at {page.viewport_size}: {page.url}",
        )

    def test_public_pages_without_javascript(self):
        engines = tuple(
            name.strip()
            for name in os.environ.get(
                "PANDECT_BROWSER_ENGINES", "chromium,webkit"
            ).split(",")
            if name.strip()
        )
        self.assertTrue(engines and not set(engines) - {"chromium", "webkit"})
        artifacts = Path(
            os.environ.get(
                "PANDECT_BROWSER_ARTIFACTS", "data/validation/marketing-browser"
            )
        ).resolve()
        artifacts.mkdir(parents=True, exist_ok=True)
        pages = ("home", "product", "trust", "roadmap", "hosting")
        viewports = ((1440, 900), (1440, 1024), (768, 1024), (390, 844), (320, 844))
        with sync_playwright() as playwright:
            for engine in engines:
                with self.subTest(engine=engine):
                    browser = getattr(playwright, engine).launch()
                    context = browser.new_context(
                        java_script_enabled=False, color_scheme="light"
                    )
                    page = context.new_page()
                    tab_key = (
                        "Alt+Tab"
                        if engine == "webkit" and sys.platform == "darwin"
                        else "Tab"
                    )
                    local_links = set()
                    for name in pages:
                        response = page.goto(self.live_server_url + reverse(name))
                        self.assertEqual(response.status, 200)
                        local_links.update(
                            page.locator('a[href^="/"]').evaluate_all(
                                "links => links.map(link => link.getAttribute('href'))"
                            )
                        )
                        self.assertTrue(
                            page.locator('a[href^="#"]').evaluate_all(
                                "links => links.every(link => "
                                "document.getElementById(link.hash.slice(1)))"
                            )
                        )
                        expect(page.get_by_role("heading", level=1)).to_have_count(1)
                        if name != "home":
                            expect(
                                page.locator("header [aria-current='page']")
                            ).to_have_attribute("href", reverse(name))
                        self.assertTrue(
                            page.locator("img").evaluate_all(
                                "images => images.every(image => "
                                "image.complete && image.naturalWidth > 0)"
                            )
                        )
                        for width, height in viewports:
                            page.set_viewport_size({"width": width, "height": height})
                            self.assert_reflow(page)
                            page.screenshot(
                                path=str(
                                    artifacts / f"{engine}-{name}-{width}x{height}.png"
                                ),
                                full_page=True,
                            )
                        page.locator("html").evaluate(
                            "element => element.style.fontSize = '200%'"
                        )
                        self.assert_reflow(page)
                        page.screenshot(
                            path=str(artifacts / f"{engine}-{name}-text-200.png"),
                            full_page=True,
                        )

                    for href in local_links:
                        response = context.request.get(self.live_server_url + href)
                        self.assertLess(response.status, 400, href)

                    page.set_viewport_size({"width": 390, "height": 844})
                    page.goto(self.live_server_url + reverse("home"))
                    header_positions = page.locator("header a").evaluate_all(
                        "links => links.map(link => { const rect = "
                        "link.getBoundingClientRect(); return {"
                        "x: rect.x, y: rect.y + rect.height / 2}; })"
                    )
                    for previous, current in pairwise(header_positions):
                        self.assertGreaterEqual(current["y"], previous["y"] - 1)
                        if abs(current["y"] - previous["y"]) < 1:
                            self.assertGreater(current["x"], previous["x"])
                    page.keyboard.press(tab_key)
                    expect(
                        page.get_by_role("link", name="Skip to content")
                    ).to_be_focused()
                    page.keyboard.press("Enter")
                    expect(page.locator("#main-content")).to_be_focused()
                    for name, route in (
                        ("Product", "product"),
                        ("Trust", "trust"),
                        ("Roadmap", "roadmap"),
                        ("Hosting", "hosting"),
                        ("Sign in", "account_login"),
                    ):
                        page.goto(self.live_server_url + reverse("home"))
                        link = page.locator("header").get_by_role(
                            "link", name=name, exact=True
                        )
                        for _ in range(12):
                            page.keyboard.press(tab_key)
                            if link.evaluate(
                                "element => element === document.activeElement"
                            ):
                                break
                        expect(link).to_be_focused()
                        self.assertEqual(
                            link.evaluate(
                                "element => getComputedStyle(element).outlineWidth"
                            ),
                            "3px",
                        )
                        page.keyboard.press("Enter")
                        expect(page).to_have_url(self.live_server_url + reverse(route))

                    page.goto(self.live_server_url + reverse("hosting"))
                    faqs = page.locator(".marketing-faq details")
                    self.assertGreater(faqs.count(), 0)
                    for index in range(faqs.count()):
                        faq = faqs.nth(index)
                        summary = faq.locator("summary")
                        summary.focus()
                        summary.press("Enter")
                        expect(faq.locator("p")).to_be_visible()
                        summary.press("Enter")
                        expect(faq.locator("p")).not_to_be_visible()

                    page.goto(self.live_server_url + reverse("home"))
                    for appearance in ("dark", "reduced-motion", "forced-colors"):
                        if appearance == "forced-colors" and engine != "chromium":
                            continue
                        page.emulate_media(
                            color_scheme="dark" if appearance == "dark" else "light",
                            reduced_motion="reduce"
                            if appearance == "reduced-motion"
                            else "no-preference",
                            forced_colors="active"
                            if appearance == "forced-colors"
                            else "none",
                        )
                        self.assert_reflow(page)
                        if appearance == "forced-colors":
                            expect(
                                page.locator("header .wordmark-fallback")
                            ).to_be_visible()
                        page.screenshot(
                            path=str(artifacts / f"{engine}-home-{appearance}.png"),
                            full_page=True,
                        )
                    context.close()
                    browser.close()

"""Public-page browser checks: python manage.py test marketing.browser."""

import os
import sys
from itertools import pairwise
from urllib.parse import unquote

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

from config.tests.browser_artifacts import artifact_directory
from core.evidence.types import KINDS


class MarketingBrowserTests(StaticLiveServerTestCase):
    def assert_reflow(self, page):
        self.assertTrue(
            page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth + 1"
            ),
            f"Horizontal overflow at {page.viewport_size}: {page.url}",
        )

    def assert_control_sizes(self, page):
        undersized = page.locator(
            "header a, footer a, .marketing-button, .text-link, .page-links a, summary"
        ).evaluate_all(
            "elements => elements.flatMap(element => { const rect = "
            "element.getBoundingClientRect(); return rect.width < 44 || "
            "rect.height < 44 ? [element.textContent.trim()] : []; })"
        )
        self.assertEqual(undersized, [], f"Small controls on {page.url}")

    def test_public_pages_without_javascript(self):
        engines = tuple(
            name.strip()
            for name in os.environ.get(
                "PANDECT_BROWSER_ENGINES", "chromium,webkit"
            ).split(",")
            if name.strip()
        )
        self.assertTrue(engines and not set(engines) - {"chromium", "webkit"})
        artifacts = artifact_directory("marketing-browser")
        pages = ("home", "product", "evidence", "trust", "roadmap", "hosting")
        viewports = (
            (1440, 900),
            (1440, 1024),
            (768, 1024),
            (390, 844),
            (360, 900),
            (320, 844),
        )
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
                        response = page.goto(
                            self.live_server_url + reverse(f"marketing:{name}")
                        )
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
                        if name == "evidence":
                            page.locator("#main-content").focus()
                            self.assertCountEqual(
                                page.locator(".catalogue-section[id]").evaluate_all(
                                    "sections => sections.map(s => s.id)"
                                ),
                                KINDS,
                            )
                            self.assertTrue(
                                page.locator(".catalogue-table").evaluate_all(
                                    "tables => tables.length > 0 && tables.every(t => "
                                    "t.caption?.textContent.trim() && t.tHead && "
                                    "t.tBodies.length && Array.from(t.tBodies[0].rows)"
                                    ".every(r => r.querySelector('th[scope=row]')))"
                                )
                            )
                        if name != "home":
                            expect(
                                page.locator("header [aria-current='page']")
                            ).to_have_attribute("href", reverse(f"marketing:{name}"))
                        self.assertTrue(
                            page.locator("img").evaluate_all(
                                "images => images.every(image => "
                                "image.complete && image.naturalWidth > 0)"
                            )
                        )
                        for width, height in viewports:
                            page.set_viewport_size({"width": width, "height": height})
                            self.assert_reflow(page)
                            self.assert_control_sizes(page)
                            if name == "evidence" and width <= 768:
                                labels = page.locator(".catalogue-cell-label")
                                self.assertGreater(labels.count(), 0)
                                self.assertTrue(
                                    labels.evaluate_all(
                                        "labels => labels.every(label => "
                                        "label.getBoundingClientRect().height > 0)"
                                    )
                                )
                                self.assertTrue(
                                    page.locator(
                                        ".catalogue-table tbody tr"
                                    ).evaluate_all(
                                        "rows => rows.every(row => Math.abs("
                                        "row.querySelector('th').getBoundingClientRect()"
                                        ".width - row.getBoundingClientRect()"
                                        ".width) < 1)"
                                    ),
                                    "Stacked headings must use the full row width",
                                )
                            page.screenshot(
                                path=str(
                                    artifacts / f"{engine}-{name}-{width}x{height}.png"
                                ),
                                full_page=name != "evidence",
                            )
                            if name == "evidence" and width in (1440, 390):
                                page.locator(".catalogue-section[id]").first.screenshot(
                                    path=str(
                                        artifacts
                                        / (
                                            f"{engine}-evidence-section-"
                                            f"{width}x{height}.png"
                                        )
                                    )
                                )
                        page.locator("html").evaluate(
                            "element => element.style.fontSize = '200%'"
                        )
                        self.assert_reflow(page)
                        page.screenshot(
                            path=str(artifacts / f"{engine}-{name}-text-200.png"),
                            full_page=name != "evidence",
                        )
                        page.locator("html").evaluate(
                            "element => element.style.removeProperty('font-size')"
                        )
                        if name == "evidence":
                            # A 360 CSS-pixel viewport exercises the reflow area
                            # available at 400% zoom on a 1440-pixel display.
                            page.set_viewport_size({"width": 360, "height": 225})
                            self.assert_reflow(page)
                            self.assert_control_sizes(page)
                    for href in sorted(local_links):
                        path, _, fragment = href.partition("#")
                        response = context.request.get(self.live_server_url + path)
                        self.assertLess(response.status, 400, href)
                        if fragment:
                            page.goto(self.live_server_url + href)
                            self.assertTrue(
                                page.evaluate(
                                    "id => document.getElementById(id) !== null",
                                    unquote(fragment),
                                ),
                                href,
                            )

                    page.set_viewport_size({"width": 390, "height": 844})
                    page.goto(self.live_server_url + reverse("marketing:home"))
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
                    analysis_url = reverse("marketing:product") + "#analysis"
                    for scope, name, href in (
                        ("header", "Product", reverse("marketing:product")),
                        (
                            "header",
                            "Evidence & analysis",
                            reverse("marketing:evidence"),
                        ),
                        ("header", "Trust", reverse("marketing:trust")),
                        ("header", "Roadmap", reverse("marketing:roadmap")),
                        ("header", "Hosting", reverse("marketing:hosting")),
                        ("header", "Sign in", reverse("account_login")),
                        (
                            "main",
                            "Explore the product",
                            reverse("marketing:product"),
                        ),
                        (
                            "main",
                            "Explore current features",
                            reverse("marketing:product") + "#available",
                        ),
                        (
                            "main",
                            "See service requirements",
                            reverse("marketing:product") + "#configured-services",
                        ),
                        ("main", "Read the roadmap", reverse("marketing:roadmap")),
                        ("main", "See analysis examples", analysis_url),
                    ):
                        page.goto(self.live_server_url + reverse("marketing:home"))
                        link = page.locator(scope).get_by_role(
                            "link", name=name, exact=True
                        )
                        for _ in range(page.get_by_role("link").count() + 1):
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
                        expect(page).to_have_url(self.live_server_url + href)
                        if "#" in href:
                            expect(page.locator(":target")).to_be_in_viewport()
                    expect(page.locator("#analysis:target")).to_be_in_viewport()

                    page.goto(self.live_server_url + reverse("marketing:evidence"))
                    for kind in KINDS:
                        link = page.locator(f'.page-links a[href="#{kind}"]')
                        link.focus()
                        link.press("Enter")
                        expect(page).to_have_url(
                            self.live_server_url
                            + reverse("marketing:evidence")
                            + f"#{kind}"
                        )
                        expect(page.locator(f"#{kind}:target")).to_be_in_viewport()

                    page.goto(self.live_server_url + reverse("marketing:hosting"))
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
                        appearance_pages = (
                            ("home", "evidence")
                            if appearance == "reduced-motion"
                            else pages
                        )
                        for name in appearance_pages:
                            page.goto(
                                self.live_server_url + reverse(f"marketing:{name}")
                            )
                            self.assert_reflow(page)
                            if appearance == "forced-colors":
                                expect(
                                    page.locator("header .wordmark-fallback")
                                ).to_be_visible()
                            page.screenshot(
                                path=str(
                                    artifacts / f"{engine}-{name}-{appearance}.png"
                                ),
                                full_page=name != "evidence",
                            )
                    context.close()
                    browser.close()

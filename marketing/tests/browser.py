"""Public-page browser checks: python manage.py test marketing.tests.browser."""

import os
import sys
from itertools import pairwise
from urllib.parse import unquote

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

from config.tests.browser_artifacts import artifact_directory
from core.evidence.types import KINDS
from marketing.catalogue import ANALYSES, IMPORTS, REPORTS


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
            "header a, footer a, .marketing-button, .text-link, .page-links a, "
            ".catalogue-families a, summary"
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
        investigation_filters = {
            "investigation_incidents": "",
            "investigation_osint": "?type=documents",
            "investigation_communications": "?type=communications",
            "investigation_vehicle_location": "?type=locations",
        }
        pages = ("home", "evidence", "trust", "roadmap", "investigations")
        pages += tuple(investigation_filters)
        investigations_path = reverse("marketing:investigations")
        evidence_path = reverse("marketing:evidence")
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
                        expect(page.locator('a[href="/hosting/"]')).to_have_count(0)
                        expect(
                            page.locator("footer").get_by_role(
                                "link", name="Self-hosting docs"
                            )
                        ).to_have_attribute(
                            "href",
                            "https://github.com/aaronspindler/pandect/blob/main/docs/README.md",
                        )
                        if name == "evidence":
                            page.locator("#main-content").focus()
                            self.assertCountEqual(
                                page.locator(".catalogue-profile").evaluate_all(
                                    "profiles => profiles.map(profile => profile.id)"
                                ),
                                KINDS,
                            )
                            self.assertCountEqual(
                                page.locator(".catalogue-method").evaluate_all(
                                    "methods => methods.map(method => method.id)"
                                ),
                                [
                                    f"{category}-{identifier}"
                                    for category, inventory in (
                                        ("analysis", ANALYSES),
                                        ("reports", REPORTS),
                                        ("imports", IMPORTS),
                                    )
                                    for identifier in inventory
                                ],
                            )
                            expect(
                                page.locator(".catalogue-families [aria-current]")
                            ).to_have_text("All analysis")
                        if name in investigation_filters:
                            breadcrumb = page.get_by_role(
                                "navigation", name="Breadcrumb"
                            )
                            expect(
                                breadcrumb.get_by_role(
                                    "link", name="All investigation types"
                                )
                            ).to_have_attribute("href", investigations_path)
                            expect(
                                breadcrumb.locator("[aria-current='page']")
                            ).to_have_text(
                                page.get_by_role("heading", level=1).inner_text()
                            )
                            expect(
                                page.get_by_text("Fictional example")
                            ).to_be_visible()
                            formats = evidence_path + investigation_filters[name]
                            expect(
                                page.locator(f'main a[href="{formats}#formats"]')
                            ).to_be_visible()
                        else:
                            current_scope = "footer" if name == "roadmap" else "header"
                            expect(
                                page.locator(f"{current_scope} [aria-current='page']")
                            ).to_have_attribute("href", reverse(f"marketing:{name}"))
                        if name == "investigations":
                            for investigation in investigation_filters:
                                path = reverse(f"marketing:{investigation}")
                                expect(
                                    page.locator(f'main a[href="{path}"]')
                                ).to_be_visible()
                        expect(
                            page.locator("header").get_by_role(
                                "link", name="Roadmap", exact=True
                            )
                        ).to_have_count(0)
                        expect(
                            page.locator("footer").get_by_role(
                                "link", name="Roadmap", exact=True
                            )
                        ).to_have_count(1)
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
                            if name == "evidence":
                                summaries = page.locator(".catalogue-method > summary")
                                self.assertTrue(
                                    summaries.evaluate_all(
                                        "summaries => summaries.every(summary => "
                                        "summary.getBoundingClientRect().height > 0)"
                                    ),
                                    "Every method stays visible at every viewport",
                                )
                            page.locator("header").scroll_into_view_if_needed()
                            page.screenshot(
                                path=str(
                                    artifacts / f"{engine}-{name}-{width}x{height}.png"
                                ),
                                full_page=name != "evidence",
                            )
                            if name == "evidence" and width in (1440, 390):
                                method = page.locator(".catalogue-method").first
                                method.locator("summary").click()
                                method.screenshot(
                                    path=str(
                                        artifacts
                                        / (
                                            f"{engine}-evidence-method-"
                                            f"{width}x{height}.png"
                                        )
                                    )
                                )
                                method.locator("summary").click()
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
                    for scope, name, href in (
                        ("header", "Overview", reverse("marketing:home")),
                        ("header", "Investigations", investigations_path),
                        (
                            "header",
                            "Evidence & analysis",
                            reverse("marketing:evidence"),
                        ),
                        ("header", "Trust", reverse("marketing:trust")),
                        ("header", "Sign in", reverse("account_login")),
                        ("footer", "Roadmap", reverse("marketing:roadmap")),
                        (
                            "main",
                            "Explore capabilities",
                            reverse("marketing:evidence"),
                        ),
                        (
                            "main",
                            "See it in practice",
                            reverse("marketing:home") + "#analysis",
                        ),
                        (
                            "main",
                            "Analysis setup requirements",
                            reverse("marketing:evidence") + "#requirements",
                        ),
                        (
                            "main",
                            "Evidence processing and privacy",
                            reverse("marketing:trust") + "#processing",
                        ),
                        (
                            "main",
                            "Explore investigation workflows",
                            investigations_path,
                        ),
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

                    investigations_url = self.live_server_url + investigations_path
                    page.goto(investigations_url)
                    osint_path = reverse("marketing:investigation_osint")
                    page.locator(f'main a[href="{osint_path}"]').press("Enter")
                    expect(page).to_have_url(self.live_server_url + osint_path)
                    page.get_by_role("link", name="All investigation types").press(
                        "Enter"
                    )
                    expect(page).to_have_url(investigations_url)

                    for fragment in (
                        "available",
                        "analysis",
                        "configured-services",
                        "limits",
                    ):
                        page.goto(
                            self.live_server_url
                            + reverse("marketing:product")
                            + f"#{fragment}"
                        )
                        expect(page).to_have_url(
                            self.live_server_url
                            + reverse("marketing:home")
                            + f"#{fragment}"
                        )
                        expect(page.locator(f"#{fragment}:target")).to_be_in_viewport()

                    catalogue_url = self.live_server_url + reverse("marketing:evidence")
                    page.goto(catalogue_url)
                    for kind in KINDS:
                        page.goto(catalogue_url + f"#{kind}")
                        profile = page.locator(f".catalogue-profile#{kind}")
                        expect(profile).to_be_in_viewport()
                        profile.locator("summary").focus()
                        profile.locator("summary").press("Enter")
                        expect(profile).to_have_attribute("open", "")
                        profile.locator("summary").press("Enter")
                        expect(profile).not_to_have_attribute("open", "")

                    page.goto(catalogue_url)
                    families = page.locator(".catalogue-families")
                    messages = families.get_by_role(
                        "link", name="Messages & calls", exact=True
                    )
                    messages.focus()
                    messages.press("Enter")
                    expect(page).to_have_url(catalogue_url + "?type=communications")
                    expect(families.locator("[aria-current]")).to_have_text(
                        "Messages & calls"
                    )
                    expect(page.locator("#analysis-colocation")).to_have_count(0)
                    expect(page.locator(".catalogue-profile")).to_have_count(3)
                    expect(page.locator("#reports-calls")).to_be_visible()
                    expect(page.locator("#imports-imessage_import")).to_be_visible()
                    summary_method = page.locator("#analysis-session_summaries")
                    summary = summary_method.locator("summary")
                    expect(summary).to_contain_text("Requires AI setup")
                    expect(summary_method).not_to_have_attribute("open", "")
                    summary.focus()
                    summary.press("Enter")
                    expect(summary_method).to_have_attribute("open", "")
                    detail = summary_method.locator(".catalogue-method-detail")
                    expect(detail).to_be_visible()
                    expect(detail).to_contain_text(
                        ANALYSES["session_summaries"]["detail"]
                    )
                    self.assertEqual(
                        summary.evaluate(
                            "element => getComputedStyle(element).outlineWidth"
                        ),
                        "3px",
                    )
                    for width, height in viewports:
                        page.set_viewport_size({"width": width, "height": height})
                        self.assert_reflow(page)
                        self.assert_control_sizes(page)
                        if width in (1440, 390):
                            page.locator("header").scroll_into_view_if_needed()
                            page.screenshot(
                                path=str(
                                    artifacts
                                    / f"{engine}-communications-{width}x{height}.png"
                                )
                            )
                    type_link = summary_method.get_by_role(
                        "link", name="Text message", exact=True
                    )
                    type_link.focus()
                    type_link.press("Enter")
                    expect(page.locator("#text-message:target")).to_be_in_viewport()
                    page.go_back()
                    expect(page).to_have_url(catalogue_url + "?type=communications")
                    families.get_by_role("link", name="Images", exact=True).click()
                    expect(page).to_have_url(catalogue_url + "?type=images")
                    expect(families.locator("[aria-current]")).to_have_text("Images")
                    expect(page.locator("#analysis-plate_ocr")).to_be_visible()
                    expect(page.locator("#analysis-session_summaries")).to_have_count(0)
                    page.go_back()
                    expect(page).to_have_url(catalogue_url + "?type=communications")
                    expect(families.locator("[aria-current]")).to_have_text(
                        "Messages & calls"
                    )
                    page.goto(catalogue_url + "?type=unknown")
                    expect(families.locator("[aria-current]")).to_have_text(
                        "All analysis"
                    )
                    expect(page.locator(".catalogue-profile")).to_have_count(len(KINDS))
                    families.get_by_role(
                        "link", name="All analysis", exact=True
                    ).click()
                    expect(page).to_have_url(catalogue_url)

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
                        page.goto(catalogue_url + "?type=communications")
                        self.assert_reflow(page)
                        self.assert_control_sizes(page)
                        expect(
                            page.locator(".catalogue-families [aria-current]")
                        ).to_have_text("Messages & calls")
                        page.screenshot(
                            path=str(
                                artifacts / f"{engine}-communications-{appearance}.png"
                            )
                        )
                    context.close()
                    browser.close()

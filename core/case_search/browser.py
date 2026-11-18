"""Keyboard, reflow and progressively enhanced case search."""

from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from people.models import Person


class CaseSearchBrowserTests(BrowserTestCase):
    artifact_subdirectory = "case-search"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("case-search-owner")
        self.case = Case.objects.create(owner=self.owner, name="Harbour records")
        self.person = Person.objects.create(
            case=self.case, first_name="Evelyn", last_name="Rivers"
        )

    def test_quick_matches_and_search_navigation(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                expect(page.get_by_label("Search this case", exact=True)).to_have_count(
                    0
                )
                page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                box = page.get_by_role("combobox", name="Search this case")
                page.keyboard.press("Control+k")
                expect(box).to_be_focused()
                suggestions = "**/search/suggestions/**"
                page.route(suggestions, lambda route: route.fulfill(status=503))
                box.fill("Unavailable")
                feedback = page.get_by_text(
                    "Quick matches unavailable. Submit for full results."
                )
                expect(feedback).to_be_visible()
                box.press("Escape")
                expect(feedback).to_have_count(0)
                expect(box).to_have_value("Unavailable")
                page.unroute(suggestions)
                box.fill("Evel")
                option = page.get_by_role("option", name="Evelyn Rivers Person")
                expect(option).to_be_visible()
                box.press("ArrowDown")
                expect(option).to_have_attribute("aria-selected", "true")
                self.capture(page, engine, "quick-matches")
                box.press("Escape")
                expect(option).to_have_count(0)
                box.fill("Rivers")
                expect(option).to_be_visible()
                box.press("ArrowDown")
                box.press("Enter")
                expect(
                    page.get_by_role("heading", name="Evelyn Rivers", exact=True)
                ).to_be_visible()
                box.fill("Evelyn")
                box.press("Enter")
                expect(
                    page.get_by_role("heading", name="1 keyword match")
                ).to_be_visible()
                page.get_by_text("Filters and sorting", exact=True).click()
                page.get_by_label("Record type").select_option("person")
                page.get_by_role("button", name="Apply filters").click()
                expect(page.get_by_label("Record type")).to_have_value("person")
                self.assertIn("type=person", page.url)
                hit = (
                    page.locator(".case-search-hit")
                    .filter(has_text="Evelyn Rivers")
                    .first
                )
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    expect(hit).to_be_visible()
                    self.capture(page, engine, f"results-{width}x{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "dark-mobile")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 321
                )
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "forced-colors")
                hit.click()
                page.go_back()
                expect(page.get_by_label("Record type")).to_have_value("person")
                expect(box).to_have_value("Evelyn")

    def test_no_javascript_submit_and_clear(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:analysis:index", case_id=self.case.pk))
                box = page.get_by_role("searchbox", name="Search this case")
                box.fill("no-such-person")
                page.get_by_role("button", name="Find in this case").click()
                expect(
                    page.get_by_role("heading", name="0 keyword matches")
                ).to_be_visible()
                box.fill("Evelyn")
                box.press("Enter")
                expect(page.locator(".case-search-hit mark").first).to_have_text(
                    "Evelyn"
                )
                page.get_by_role("link", name="Clear search", exact=True).click()
                expect(box).to_have_value("")

"""Keyboard and narrow-screen access to case search without configured providers."""

from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase


class CaseSearchBrowserTests(BrowserTestCase):
    artifact_subdirectory = "case-search"

    def test_search_status_and_filters(self):
        self.owner = self.make_user("search-owner")
        case = Case.objects.create(owner=self.owner, name="Harbour records")
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.url("core:search:results", case_id=case.pk))
                expect(
                    page.get_by_text("External AI is disabled for this case.")
                ).to_be_visible()
                evidence = page.get_by_role("link", name="Browse case evidence")
                expect(evidence).to_be_visible()
                page.get_by_text("Filter passages").click()
                expect(page.get_by_label("Record type")).to_be_visible()
                page.set_viewport_size({"width": 320, "height": 844})
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 321
                )
                self.capture(page, engine, "case-search-filters")
                evidence.press("Enter")
                expect(
                    page.get_by_role("heading", name="Evidence", exact=True)
                ).to_be_visible()

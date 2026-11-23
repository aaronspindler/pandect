"""Keyboard, reflow and progressively enhanced case search."""

from itertools import product
from unittest.mock import patch

from django.test import override_settings
from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from core.analysis.errors import AnalysisError
from core.analysis.tests.test_gateway import CONFIG
from core.case_search.indexing import index_case_passages
from core.models import CaseSearchIndex
from people.models import Person
from text.models import TextEvidence


class CaseSearchBrowserTests(BrowserTestCase):
    artifact_subdirectory = "case-search"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("case-search-owner")
        self.case = Case.objects.create(owner=self.owner, name="Harbour records")
        self.person = Person.objects.create(
            case=self.case, first_name="Evelyn", last_name="Rivers"
        )

    @override_settings(
        PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway", PANDECT_AI_GATEWAY=CONFIG
    )
    def test_hosted_approval_resume_and_first_related_result(self):
        def run_work(case):
            for _ in range(12):
                index = CaseSearchIndex.objects.get(case=case)
                if index.status in {"ready", "failed"}:
                    return index.status
                index_case_passages.call(str(index.pk), str(index.token))
            self.fail("Index did not finish its bounded synthetic work.")

        def vectors(texts, authorize, **kwargs):
            authorize()
            return [[1.0] + [0.0] * 1535 for _ in texts]

        for engine, javascript in product(self.engines, (True, False)):
            case = Case.objects.create(
                owner=self.owner, name=f"Harbour inspection {engine}"
            )
            note = TextEvidence.objects.create(
                case=case,
                uploaded_by=self.owner,
                title="Harbour interview",
                body="The boat captain delayed the inspection until tomorrow.",
            )
            with (
                self.subTest(engine=engine, javascript=javascript),
                self.browser_page(engine, java_script_enabled=javascript) as (_, page),
            ):
                self.sign_in(page)
                page.goto(
                    self.url("cases:case_settings", case_id=case.pk) + "#meaning-search"
                )
                approve = page.get_by_role("button", name="Approve and start indexing")
                expect(approve).to_be_visible()
                if javascript:
                    self.check_approval_layout(page, engine, approve)
                approve.press("Enter")
                page.wait_for_url("**/processing/#case-search-index")
                with patch(
                    "core.case_search.indexing.embed",
                    side_effect=AnalysisError("gateway_authentication"),
                ):
                    self.assertEqual(self.db(run_work, case), "failed")
                page.reload()
                resume = page.get_by_role("button", name="Resume indexing")
                expect(resume).to_be_visible()
                if javascript:
                    self.capture(page, engine, "hosted-stopped")
                resume.click()
                expect(resume).to_have_count(0)
                with patch("core.case_search.indexing.embed", side_effect=vectors):
                    self.assertEqual(self.db(run_work, case), "ready")
                with patch(
                    "core.case_search.semantic.embed", side_effect=vectors
                ) as query:
                    page.goto(
                        self.url("core:search:results", case_id=case.pk)
                        + "?q=meeting+postponed&type=text"
                    )
                    query.assert_not_called()
                    page.get_by_role("button", name="Search by meaning").click()
                    results = page.get_by_role("region", name="Related passages")
                    hit = results.get_by_role(
                        "link", name="Harbour interview", exact=False
                    )
                    expect(hit).to_be_visible()
                    self.capture(
                        page,
                        engine,
                        "hosted-results" if javascript else "hosted-results-no-js",
                    )
                    query.assert_called_once()
                    hit.click()
                    page.wait_for_url(
                        self.url(
                            "core:evidence:detail",
                            case_id=case.pk,
                            kind="text",
                            evidence_id=note.pk,
                        )
                    )

    def check_approval_layout(self, page, engine, approve):
        for width, height in (
            (1440, 900),
            (1440, 1024),
            (768, 1024),
            (390, 844),
            (320, 844),
        ):
            page.set_viewport_size({"width": width, "height": height})
            page.locator("#meaning-search").evaluate(
                "section => section.scrollIntoView()"
            )
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"), width + 1
            )
            self.capture(page, engine, f"hosted-approval-{width}x{height}")
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        approve.focus()
        expect(approve).to_be_focused()
        self.capture(page, engine, "hosted-approval-dark")
        if engine == "chromium":
            page.emulate_media(forced_colors="active")
            self.capture(page, engine, "hosted-approval-forced-colors")
            page.emulate_media(forced_colors="none")
        page.evaluate("document.documentElement.style.fontSize = '200%'")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 321)

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
                box.fill("missing quick title")
                expect(
                    page.get_by_text("No quick matches.", exact=True)
                ).to_be_visible()
                full = page.get_by_role("option", name="Search all results")
                box.press("ArrowDown")
                expect(full).to_have_attribute("aria-selected", "true")
                box.press("Enter")
                expect(
                    page.get_by_role("heading", name="0 keyword matches")
                ).to_be_visible()
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
                page.wait_for_url("**/search/?*type=person*")
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

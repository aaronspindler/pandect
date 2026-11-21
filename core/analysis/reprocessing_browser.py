"""Synthetic selection, approval, and progress journey without JavaScript."""

from django.test import override_settings
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.tests.test_gateway import CONFIG
from core.models import AnalysisRun, CaseAnalysisBatch
from geo.models import LocationEvidence
from text.models import TextEvidence


class ReprocessingBrowserTests(BrowserTestCase):
    artifact_subdirectory = "reprocessing-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("reprocessing-owner")
        self.viewer = self.make_user("reprocessing-viewer")
        self.case = Case.objects.create(
            owner=self.owner, name="Synthetic reprocessing case"
        )
        CaseMembership.objects.create(case=self.case, user=self.viewer, role="viewer")
        TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic correspondence",
            body="The meeting date remains unconfirmed.",
        )
        LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic reported location",
            address="Synthetic harbour",
        )

    def capture_layouts(self, page, engine):
        for width, height, name in (
            (1440, 900, "desktop"),
            (1440, 1024, "tall-desktop"),
            (768, 1024, "tablet"),
            (390, 844, "mobile"),
            (320, 844, "reflow"),
        ):
            page.set_viewport_size({"width": width, "height": height})
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"), width + 1
            )
            self.capture(page, engine, f"reprocess-{name}")
        page.evaluate("document.documentElement.style.fontSize = '200%'")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 321)
        self.capture(page, engine, "reprocess-large-text")
        page.evaluate("document.documentElement.style.fontSize = ''")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        self.capture(page, engine, "reprocess-dark-reduced-motion")
        page.emulate_media(color_scheme="light", forced_colors="active")
        self.capture(page, engine, "reprocess-forced-colors")
        page.emulate_media(forced_colors="none", reduced_motion="no-preference")

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_multiselect_external_approval_and_batch_progress_without_javascript(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:analysis:processing", case_id=self.case.pk))
                page.locator("#processing-actions > summary").press("Enter")
                action = page.get_by_role("link", name="Reprocess evidence", exact=True)
                action.focus()
                expect(action).to_be_focused()
                action.press("Enter")
                expect(
                    page.get_by_role("heading", name="Reprocess evidence", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("navigation", name="Investigation").get_by_role(
                        "link", name="Processing", exact=True
                    )
                ).to_have_attribute("aria-current", "page")
                text = page.get_by_role("checkbox", name="Text · 1 active", exact=True)
                location = page.get_by_role(
                    "checkbox", name="Location · 1 active", exact=True
                )
                expect(text).to_be_checked()
                expect(location).to_be_checked()
                text.focus()
                text.press("Space")
                expect(text).not_to_be_checked()
                text.press("Space")
                location.focus()
                location.press("Space")
                expect(location).not_to_be_checked()
                location.press("Space")
                for choice in (text, location):
                    self.assertGreaterEqual(
                        choice.locator("..").bounding_box()["height"], 44
                    )
                self.capture_layouts(page, engine)

                page.get_by_role(
                    "radio", name="Local, private and external services", exact=True
                ).check()
                submit = page.get_by_role(
                    "button", name="Start reprocessing", exact=True
                )
                submit.focus()
                submit.press("Enter")
                expect(
                    page.get_by_role("heading", name="Review your selection")
                ).to_be_visible()
                expect(page.get_by_role("alert")).to_contain_text(
                    "Approve external processing to continue."
                )
                expect(text).to_be_checked()
                expect(location).to_be_checked()
                expect(
                    page.get_by_role(
                        "radio", name="Local, private and external services", exact=True
                    )
                ).to_be_checked()
                self.capture(page, engine, "reprocess-approval-required")
                approval = page.get_by_role(
                    "checkbox",
                    name="I approve external processing of the selected evidence types",
                    exact=True,
                )
                approval.focus()
                approval.press("Space")
                expect(approval).to_be_checked()
                submission_id = page.locator(
                    "input[name='submission_id']"
                ).input_value()
                submit.press("Enter")
                expect(page).to_have_url(
                    self.url("core:analysis:processing", case_id=self.case.pk)
                    + f"?batch={submission_id}#processing-history"
                )
                expect(page.locator("#processing-history")).to_contain_text(
                    "Selected batch"
                )
                batch = self.db(CaseAnalysisBatch.objects.get, pk=submission_id)
                self.assertEqual(set(batch.scope["kinds"]), {"location", "text"})
                self.assertEqual(batch.scope["mode"], "both")
                self.assertEqual(
                    self.db(AnalysisRun.objects.filter(batch=batch).count), 0
                )
                self.capture(page, engine, "reprocess-batch-progress")

    def test_viewer_has_history_without_reprocessing_action(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.url("core:analysis:processing", case_id=self.case.pk))
                expect(
                    page.get_by_role("heading", name="Processing", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name="Reprocess evidence", exact=True)
                ).to_have_count(0)
                denied = page.goto(
                    self.url("core:analysis:reprocess", case_id=self.case.pk)
                )
                self.assertEqual(denied.status, 403)

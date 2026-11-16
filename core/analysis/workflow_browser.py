"""Synthetic complete-findings and supporting-case-tool browser journeys."""

from datetime import UTC, datetime

from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.services import process_run, start_run
from core.models import AnalysisRun, CaseAnalysisBatch
from people.models import Person
from phone.models import TextMessageEvidence
from text.models import TextEvidence


class InvestigationWorkflowBrowserTests(BrowserTestCase):
    artifact_subdirectory = "investigation-workflows"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("workflow-owner")
        self.viewer = self.make_user("workflow-viewer")
        self.viewer.timezone = "America/Toronto"
        self.viewer.save(update_fields=["timezone"])
        self.case = Case.objects.create(
            owner=self.owner, name="Synthetic workflow case"
        )
        CaseMembership.objects.create(case=self.case, user=self.viewer, role="viewer")
        self.person = Person.objects.create(
            case=self.case, first_name="Synthetic participant"
        )
        self.message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Participant correspondence",
            sender=self.person,
            recipient=self.person,
            body="\n".join(f"person-{index:02}@example.test" for index in range(27))
            + "\n2026-11-15",
            occurred_at=datetime(2026, 11, 15, 12, tzinfo=UTC),
            collected_at=datetime(2026, 11, 16, 12, tzinfo=UTC),
        )
        self.note = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Separate retained text",
            body="Another source mentions person-00@example.test.",
        )
        self.batch = CaseAnalysisBatch.objects.create(
            case=self.case,
            requested_by=self.owner,
            status="complete",
            processed_sources=2,
            queued_runs=3,
        )
        for kind, source in (("text-message", self.message), ("text", self.note)):
            run, _ = start_run(
                self.owner,
                self.case.pk,
                "entities",
                [{"kind": kind, "id": str(source.pk)}],
                batch=self.batch,
            )
            process_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, "complete", run.error_code)
        start_run(
            self.owner,
            self.case.pk,
            "entities",
            [{"kind": "text", "id": str(self.note.pk)}],
            batch=self.batch,
        )

    def capture_layouts(self, page, engine, name):
        for width, height, suffix in (
            (1440, 900, "desktop"),
            (390, 844, "mobile"),
            (320, 844, "reflow"),
        ):
            page.set_viewport_size({"width": width, "height": height})
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"), width + 1
            )
            self.capture(page, engine, f"{name}-{suffix}")
        page.evaluate("document.documentElement.style.fontSize = '200%'")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 321)
        self.capture(page, engine, f"{name}-large-text")
        page.evaluate("document.documentElement.style.fontSize = ''")
        page.set_viewport_size({"width": 1440, "height": 900})

    def test_findings_person_connections_chronology_and_processing_without_javascript(
        self,
    ):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.url("cases:case_detail", case_id=self.case.pk))
                expect(page).to_have_url(
                    self.url("core:analysis:index", case_id=self.case.pk)
                )
                expect(
                    page.get_by_role("heading", name="Case Tools", exact=True)
                ).to_be_visible()
                navigation = page.get_by_role("navigation", name="Investigation")
                expect(
                    navigation.get_by_role("link", name="Case Tools", exact=True)
                ).to_have_attribute("aria-current", "page")
                expect(
                    page.get_by_role("link", name="Add evidence", exact=True)
                ).to_have_count(0)
                navigation.get_by_role("link", name="Evidence", exact=True).press(
                    "Enter"
                )
                expect(
                    navigation.get_by_role("link", name="Evidence", exact=True)
                ).to_have_attribute("aria-current", "page")
                navigation.get_by_role("link", name="Processing", exact=True).press(
                    "Enter"
                )
                expect(
                    navigation.get_by_role("link", name="Processing", exact=True)
                ).to_have_attribute("aria-current", "page")
                expect(
                    page.get_by_role("button", name="Rerun all analysis", exact=True)
                ).to_have_count(0)
                navigation.get_by_role("link", name="Case Tools", exact=True).press(
                    "Enter"
                )
                self.capture_layouts(page, engine, "case-tools")
                browse = page.get_by_role(
                    "link", name="Browse all findings", exact=True
                )
                browse.focus()
                expect(browse).to_be_focused()
                browse.press("Enter")
                expect(
                    page.get_by_role("heading", name="Browse all findings")
                ).to_be_visible()
                pages = page.get_by_role("navigation", name="Finding pages")
                pages.get_by_role("link", name="Next").press("Enter")
                expect(pages).to_contain_text("Page 2")
                self.capture_layouts(page, engine, "catalogue-page-two")
                page.get_by_label("Search findings").fill("person-00@example.test")
                page.locator(".evidence-filters > summary").press("Enter")
                page.get_by_label("Person").select_option(str(self.person.pk))
                page.get_by_role("button", name="Find findings").press("Enter")
                expect(page.locator(".analysis-finding-rows > li")).to_have_count(1)
                self.capture_layouts(page, engine, "catalogue-filtered")
                finding = page.locator(".analysis-finding-rows > li a").first
                finding.focus()
                finding.press("Enter")
                expect(page.locator("#finding-1")).to_be_visible()

                page.goto(
                    self.url(
                        "people:detail", case_id=self.case.pk, person_id=self.person.pk
                    )
                )
                expect(
                    page.get_by_role("heading", name="Related evidence", exact=True)
                ).to_be_visible()
                self.capture_layouts(page, engine, "person-related")
                page.get_by_role(
                    "heading", name="Related findings", exact=True
                ).scroll_into_view_if_needed()
                self.capture(page, engine, "person-related-findings")
                related = page.get_by_role(
                    "link", name="Browse related evidence", exact=True
                )
                related.focus()
                related.press("Enter")
                self.assertIn(f"person={self.person.pk}", page.url)
                expect(
                    page.get_by_role(
                        "link", name="Participant correspondence", exact=False
                    ).first
                ).to_be_visible()

                page.goto(self.url("core:analysis:connections", case_id=self.case.pk))
                self.capture_layouts(page, engine, "connections")
                group = (
                    page.locator(".analysis-finding-rows a")
                    .filter(has_text="person-00@example.test")
                    .first
                )
                group.focus()
                group.press("Enter")
                expect(
                    page.get_by_role("heading", name="Email occurrences", exact=True)
                ).to_be_visible()
                expect(page.locator(".analysis-finding-rows > li")).to_have_count(2)
                self.capture_layouts(page, engine, "connection-occurrences")

                page.goto(self.url("core:evidence:chronology", case_id=self.case.pk))
                expect(
                    page.get_by_text("Displayed in America/Toronto.", exact=False)
                ).to_be_visible()
                expect(
                    page.get_by_role(
                        "heading", name="Unplaced date references", exact=True
                    )
                ).to_be_visible()
                self.capture_layouts(page, engine, "chronology")
                page.locator("details > summary").filter(
                    has_text="Filter chronology"
                ).press("Enter")
                page.locator('select[name="category"]').select_option("acquired")
                page.get_by_role("button", name="Apply filters").press("Enter")
                expect(
                    page.get_by_role("heading", name="Acquisition times", exact=True)
                ).to_be_visible()

                page.goto(
                    self.url("core:analysis:processing", case_id=self.case.pk)
                    + f"?batch={self.batch.pk}"
                )
                expect(
                    page.get_by_role("heading", name="In progress", exact=True)
                ).to_be_visible()
                expect(
                    page.locator(
                        "section[aria-labelledby='in-progress-heading'] "
                        ".analysis-history-row"
                    )
                ).to_have_count(1)
                expect(
                    page.locator("#processing-history .analysis-history-row")
                ).to_have_count(3)
                page.get_by_role("link", name="Refresh status").press("Enter")
                self.capture_layouts(page, engine, "processing")
                self.db(
                    CaseMembership.objects.filter(
                        case=self.case, user=self.viewer
                    ).delete
                )
                denied = page.goto(
                    self.url("core:analysis:browse", case_id=self.case.pk)
                )
                self.assertEqual(denied.status, 404)
                self.db(
                    CaseMembership.objects.create,
                    case=self.case,
                    user=self.viewer,
                    role="viewer",
                )
        self.assertEqual(self.db(AnalysisRun.objects.filter(case=self.case).count), 3)

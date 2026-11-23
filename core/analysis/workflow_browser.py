"""Synthetic complete-findings and supporting-case-tool browser journeys."""

import re
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from django.test import override_settings
from django.urls import reverse
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.operations import OPERATIONS
from core.analysis.services import process_run, start_run
from core.analysis.sources import source_id
from core.analysis.tests.test_execution import LOCAL
from core.models import AnalysisRun, CaseAnalysisBatch
from geo.models import PersonLocationEvidence, VehicleLocationEvidence
from geo.trip_jobs import process_trips
from geo.trip_processing import prepare_case_trips
from people.models import Person
from phone.models import PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence
from vehicle.models import Vehicle


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

    def capture_layouts(self, page, engine, name, *, section=None):
        for width, height, suffix in (
            (1440, 900, "desktop"),
            (1440, 1024, "desktop-tall"),
            (768, 1024, "tablet"),
            (390, 844, "mobile"),
            (320, 844, "reflow"),
        ):
            page.set_viewport_size({"width": width, "height": height})
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"), width + 1
            )
            if section is not None:
                section.evaluate("element => element.scrollIntoView()")
            self.capture(page, engine, f"{name}-{suffix}")
        page.evaluate("document.documentElement.style.fontSize = '200%'")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 321)
        if section is not None:
            section.evaluate("element => element.scrollIntoView()")
        self.capture(page, engine, f"{name}-large-text")
        page.evaluate("document.documentElement.style.fontSize = ''")
        page.set_viewport_size({"width": 1440, "height": 900})

    @override_settings(PANDECT_LOCAL_AI=LOCAL)
    def test_compare_claim_with_keyboard_and_no_javascript(self):
        claim_text = "Booking R41 was cancelled."
        claim = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Message about a booking",
            sender=self.person,
            recipient=self.person,
            body=f"Tuesday update: {claim_text}",
        )
        response_quote = "Reservation R41 has been cancelled."
        comparison = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Booking cancellation notice",
            body=f"Automatic confirmation. {response_quote}",
        )
        mock_reply = {
            "items": [
                {
                    "source_id": source_id({"kind": "text", "id": comparison.pk}),
                    "status": "supporting",
                    "quote": response_quote,
                    "reason": "The notice reports cancellation for the same booking.",
                }
            ]
        }
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:analysis:create", case_id=self.case.pk))
                page.get_by_role("link", name="Compare a claim", exact=True).press(
                    "Enter"
                )
                for title in (claim.title, comparison.title):
                    control = page.get_by_role("checkbox", name=title)
                    control.focus()
                    expect(control).to_be_focused()
                    control.press("Space")
                    expect(control).to_be_checked()
                page.get_by_role("button", name="Choose claim", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role("heading", name="Choose the claim")
                ).to_be_visible()
                page.get_by_label("Claim source", exact=True).select_option(
                    source_id({"kind": "text-message", "id": claim.pk})
                )
                quotation = page.get_by_label("Exact quotation", exact=True)
                quotation.fill(claim_text)
                self.capture_layouts(page, engine, "claim-comparison-choose")
                quotation.focus()
                expect(quotation).to_be_focused()
                page.get_by_role("button", name="Compare claim", exact=True).press(
                    "Enter"
                )
                expect(page.get_by_text("Queued", exact=True).first).to_be_visible()
                run_id = UUID(page.url.rstrip("/").split("/")[-1])
                with patch(
                    "core.analysis.local.LocalClient.infer", return_value=mock_reply
                ):
                    self.db(process_run, run_id)
                page.reload()
                expect(
                    page.get_by_text("Potentially supporting", exact=True).first
                ).to_be_visible()
                expect(
                    page.get_by_text(response_quote, exact=True).first
                ).to_be_visible()
                expect(
                    page.get_by_text("AI suggestion", exact=True).first
                ).to_be_visible()
                self.capture_layouts(page, engine, "claim-comparison-result")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "claim-comparison-result-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "claim-comparison-result-forced-colors")
                page.get_by_role("link", name=comparison.title, exact=True).first.press(
                    "Enter"
                )
                expect(page.get_by_text(comparison.body, exact=True)).to_be_visible()

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
                expect(
                    page.get_by_role("link", name="Selections", exact=True)
                ).to_have_count(0)
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
                    page.get_by_role("button", name="Run local checks", exact=True)
                ).to_have_count(0)
                navigation.get_by_role("link", name="Case Tools", exact=True).press(
                    "Enter"
                )
                self.capture_layouts(page, engine, "case-tools")
                browse = page.get_by_role("link", name="Findings", exact=True)
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
                expect(page.get_by_label("Person")).to_be_hidden()
                page.locator(".evidence-filters > summary").press("Enter")
                expect(page.get_by_label("Person")).to_be_visible()
                expect(page.get_by_label("Result history")).to_be_visible()
                page.get_by_label("Person").select_option(str(self.person.pk))
                page.get_by_role("button", name="Apply filters", exact=True).press(
                    "Enter"
                )
                expect(page.locator(".analysis-finding-rows > li")).to_have_count(1)
                self.capture_layouts(page, engine, "catalogue-filtered")
                finding = page.locator(".analysis-finding-rows > li a").first
                finding.focus()
                finding.press("Enter")
                expect(page.locator("#finding-1")).to_be_visible()
                page.get_by_role("link", name="Back to findings", exact=True).press(
                    "Enter"
                )
                expect(page.get_by_label("Search findings")).to_have_value(
                    "person-00@example.test"
                )
                expect(page.get_by_label("Person")).to_have_value(str(self.person.pk))
                expect(page.get_by_label("Person")).to_be_hidden()

                page.goto(
                    self.url(
                        "people:detail", case_id=self.case.pk, person_id=self.person.pk
                    )
                )
                expect(
                    page.get_by_role("heading", name="Related evidence", exact=True)
                ).to_be_visible()
                self.capture_layouts(page, engine, "person-related")
                page.get_by_role("link", name=re.compile(r"^Findings · ")).press(
                    "Enter"
                )
                expect(page).to_have_url(re.compile(r"#related-findings$"))
                self.capture(page, engine, "person-related-findings")
                related = page.get_by_role(
                    "link", name="Browse related evidence", exact=True
                )
                related.focus()
                related.press("Enter")
                self.assertIn(f"person={self.person.pk}", page.url)
                message_path = reverse(
                    "core:evidence:detail",
                    kwargs={
                        "case_id": self.case.pk,
                        "kind": "text-message",
                        "evidence_id": self.message.pk,
                    },
                )
                message_row = page.locator(
                    f'a.evidence-row[href="{message_path}'
                    f'?person={self.person.pk}&state=all"]'
                )
                expect(message_row).to_be_visible()
                expect(message_row).to_contain_text("person-00@example.test")
                expect(message_row).to_contain_text(self.person.first_name)
                message_row.press("Enter")
                page.get_by_role("navigation", name="Evidence actions").get_by_role(
                    "link", name=re.compile(r"^Findings \(\d+\)$")
                ).press("Enter")
                expect(page).to_have_url(re.compile(r"#evidence-findings-heading$"))

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
                self.capture_layouts(page, engine, "chronology")
                page.get_by_role("link", name="Show UTC times").press("Enter")
                expect(
                    page.get_by_text("Displayed in UTC.", exact=False)
                ).to_be_visible()
                page.get_by_role("link", name="Show local times").press("Enter")
                page.get_by_role(
                    "link", name="Extracted date references", exact=True
                ).press("Enter")
                expect(
                    page.get_by_role(
                        "heading", name="Extracted date references", exact=True
                    )
                ).to_be_visible()
                self.capture_layouts(page, engine, "extracted-date-references")
                page.get_by_role("link", name="Back to chronology").press("Enter")
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
                ).to_have_count(0)
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
            with self.browser_page(engine, java_script_enabled=False) as (_, page):
                self.sign_in(page, self.owner)
                page.goto(self.url("core:analysis:browse", case_id=self.case.pk))
                expect(
                    page.get_by_role("link", name="Save to selection")
                ).to_have_count(0)
                self.capture_layouts(page, engine, "catalogue-owner")
                page.goto(self.url("core:analysis:processing", case_id=self.case.pk))
                page.locator("#processing-actions > summary").press("Enter")
                expect(
                    page.get_by_role("link", name="Choose analysis", exact=True)
                ).to_be_visible()
                self.capture_layouts(page, engine, "processing-owner")
        self.assertEqual(self.db(AnalysisRun.objects.filter(case=self.case).count), 3)

    def test_unplaced_original_and_relative_claim_without_javascript(self):
        note = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Unscheduled pickup",
            body="The pickup was moved to tomorrow.",
        )
        run, _ = start_run(
            self.owner, self.case.pk, "entities", [{"kind": "text", "id": str(note.pk)}]
        )
        process_run(run.pk)
        before = AnalysisRun.objects.filter(case=self.case).count()
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(
                    self.url("core:evidence:chronology", case_id=self.case.pk)
                    + "?view=day"
                )
                link = page.get_by_role(
                    "link", name="Unplaced evidence and time claims"
                )
                link.focus()
                expect(link).to_be_focused()
                link.press("Enter")
                expect(
                    page.get_by_role(
                        "heading", name="Unplaced evidence and time claims"
                    )
                ).to_be_visible()
                claim = page.get_by_role("link", name="Relative date", exact=True)
                expect(page.get_by_text("tomorrow", exact=True)).to_be_visible()
                self.capture_layouts(page, engine, "unplaced")
                page.set_viewport_size({"width": 390, "height": 844})
                jump = page.get_by_role("navigation", name="Jump to section")
                jump.get_by_role("link", name=re.compile("Time claims")).press("Enter")
                expect(page.locator("#unplaced-dates")).to_be_focused()
                self.capture(page, engine, "unplaced-mobile-claims")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "unplaced-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "unplaced-forced-colors")
                page.emulate_media(forced_colors="none")
                claim.focus()
                expect(claim).to_be_focused()
                claim.press("Enter")
                expect(page.get_by_text("tomorrow", exact=True).first).to_be_visible()
                page.go_back()
                page.locator("section[aria-labelledby='undated-heading']").get_by_role(
                    "link", name=note.title
                ).press("Enter")
                expect(
                    page.get_by_role("heading", name=note.title, exact=True)
                ).to_be_visible()
                page.go_back()
                expect(claim).to_be_visible()
        self.assertEqual(
            self.db(AnalysisRun.objects.filter(case=self.case).count), before
        )

    def test_daily_timeline_navigation_and_source_without_javascript(self):
        colleague = Person.objects.create(case=self.case, first_name="Robin")
        alex = Person.objects.create(case=self.case, first_name="Alex")
        messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title="Market Hall visit",
                sender=sender,
                recipient=recipient,
                body=body,
                occurred_at=datetime(2026, 11, 15, 10, minute, tzinfo=UTC),
            )
            for minute, sender, recipient, body in (
                (15, alex, colleague, "Meet me at the north entrance of Market Hall."),
                (18, colleague, alex, "I will bring the delivery receipt."),
            )
        ]
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "sessions",
            [{"kind": "text-message", "id": str(message.pk)} for message in messages],
        )
        process_run(run.pk)
        session_id = run.result.findings.get().pk
        van = Vehicle.objects.create(case=self.case, make="Ford", model="Transit")
        for minute, address in ((30, "10 Harbour Road"), (50, "Market Hall")):
            VehicleLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                vehicle=van,
                title="Delivery van position",
                address=address,
                observed_at=datetime(2026, 11, 15, 11, minute, tzinfo=UTC),
            )
        (generation,) = prepare_case_trips(self.owner, self.case.pk)
        for _ in range(2):
            process_trips(str(generation.pk), str(generation.token))
            generation.refresh_from_db()
        self.assertEqual(generation.status, "complete", generation.error_code)
        PersonLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Market entrance sighting",
            person=colleague,
            address="North entrance, Market Hall",
            observed_at=datetime(2026, 11, 15, 13, 20, tzinfo=UTC),
        )
        PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Afternoon call",
            sender=colleague,
            recipient=self.person,
            occurred_at=datetime(2026, 11, 15, 16, 35, tzinfo=UTC),
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.url("core:analysis:index", case_id=self.case.pk))
                page.get_by_role("link", name="Chronology", exact=True).press("Enter")
                expect(
                    page.get_by_role("heading", name="Daily timeline", exact=True)
                ).to_be_visible()
                date = page.get_by_label("Date", exact=True)
                expect(date).to_have_value("2026-11-16")
                expect(page).to_have_url(re.compile("day=2026-11-16"))
                page.get_by_role("link", name="Older dates", exact=True).press("Enter")
                expect(date).to_have_value("2026-11-15")
                page.get_by_role("link", name="Show UTC times").press("Enter")
                expect(date).to_have_value("2026-11-15")
                expect(page).to_have_url(re.compile("clock=utc"))
                self.capture_layouts(page, engine, "daily-timeline")
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator(".day-timeline").evaluate("list => list.scrollIntoView()")
                self.capture(page, engine, "daily-timeline-mobile-entries")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.evaluate("window.scrollTo(0, 0)")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "daily-timeline-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "daily-timeline-forced-colors")
                page.emulate_media(forced_colors="none")

                date.fill("2026-11-14")
                page.get_by_role("button", name="Go to date").press("Enter")
                expect(date).to_have_value("2026-11-14")
                expect(
                    page.get_by_text("No dated activity or collections on this day.")
                ).to_be_visible()
                page.get_by_role("link", name="Newer dates", exact=True).press("Enter")
                expect(date).to_have_value("2026-11-15")
                page.locator(f'a[href$="#instance-{session_id}"]').press("Enter")
                expect(page).to_have_url(re.compile(f"#instance-{session_id}$"))
                expect(page.get_by_text(messages[1].body, exact=True)).to_be_visible()
                page.go_back()
                message_link = page.locator(f'a[href$="#message-{self.message.pk}"]')
                message_link.press("Enter")
                expect(page).to_have_url(
                    re.compile(f"message={self.message.pk}#message-{self.message.pk}$")
                )
                page.go_back()
                expect(date).to_have_value("2026-11-15")
                page.get_by_role("link", name="Newer dates", exact=True).press("Enter")
                expect(date).to_have_value("2026-11-16")
                source = reverse(
                    "core:evidence:detail",
                    args=(self.case.pk, "text-message", self.message.pk),
                )
                link = page.locator(f'a[href^="{source}"]')
                link.focus()
                expect(link).to_be_focused()
                link.press("Enter")
                expect(page).to_have_url(re.compile(re.escape(source)))
                page.go_back()
                expect(date).to_have_value("2026-11-16")
                page.get_by_role("link", name="Recorded chronology", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_text("Filter chronology", exact=True)
                ).to_be_visible()

    def test_daily_timeline_scrolls_between_days_and_opens_the_exact_date(self):
        for day in (16, 20):
            for minute in range(15):
                TextEvidence.objects.create(
                    case=self.case,
                    uploaded_by=self.owner,
                    title=f"Collection on the {day} at minute {minute}",
                    body=f"Synthetic collected text for November {day}, {minute}.",
                    collected_at=datetime(2026, 11, day, 13, minute, tzinfo=UTC),
                )

        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                url = self.url("core:evidence:chronology", case_id=self.case.pk)
                page.goto(f"{url}?view=day&clock=utc")
                date = page.get_by_label("Date", exact=True)
                days = page.locator("[data-timeline-day]")
                latest_day = page.locator('[data-timeline-day="2026-11-20"]')
                earlier_day = page.locator('[data-timeline-day="2026-11-16"]')
                empty_day = page.locator('[data-timeline-day="2026-11-18"]')
                expect(date).to_have_value("2026-11-20")
                expect(days).to_have_count(1)
                expect(
                    page.get_by_role("link", name="Older dates", exact=True)
                ).to_be_hidden()
                page.locator(
                    '[data-timeline-edge="older"]'
                ).scroll_into_view_if_needed()
                expect(earlier_day).to_be_attached()
                expect(days.first).to_have_attribute("data-timeline-day", "2026-11-20")
                expect(days.nth(1)).to_have_attribute("data-timeline-day", "2026-11-16")
                expect(empty_day).to_have_count(0)
                earlier_day.evaluate("section => section.scrollIntoView()")
                expect(page).to_have_url(re.compile(r"day=2026-11-16"))
                expect(page).to_have_url(re.compile(r"clock=utc"))
                self.capture(page, engine, "daily-timeline-scrolled")

                page.goto(f"{url}?view=day&day=2026-11-20&clock=utc")
                date.focus()
                date.fill("2026-11-18")
                expect(empty_day.get_by_role("heading")).to_have_text(
                    "Wednesday, 18 November 2026"
                )
                expect(
                    empty_day.get_by_text(
                        "No dated activity or collections on this day."
                    )
                ).to_be_visible()
                expect(page).to_have_url(re.compile(r"day=2026-11-18"))
                expect(date).to_be_focused()
                page.get_by_role("button", name="Go to date").focus()

                date.fill("2026-11-16")
                expect(earlier_day.get_by_role("heading")).to_have_text(
                    "Monday, 16 November 2026"
                )
                expect(page).to_have_url(re.compile(r"day=2026-11-16"))
                expect(earlier_day.locator("time").first).to_have_text(
                    re.compile(r"\bUTC$")
                )
                expect(date).to_be_focused()
                page.mouse.move(650, 380)
                page.mouse.wheel(0, -400)
                expect(latest_day).to_be_attached()
                expect(days.first).to_have_attribute("data-timeline-day", "2026-11-20")
                latest_day.evaluate("section => section.scrollIntoView()")
                expect(date).to_have_value("2026-11-20")
                page.go_back()
                expect(page).to_have_url(re.compile(r"day=2026-11-18"))
                expect(date).to_have_value("2026-11-18")
                expect(empty_day.get_by_role("heading")).to_be_visible()
                date.focus()
                date.press("ArrowUp")
                edited_date = date.input_value()
                self.assertNotEqual(edited_date, "2026-11-18")
                expect(page).to_have_url(re.compile(f"day={re.escape(edited_date)}"))
                expect(date).to_be_focused()

    def test_failed_daily_timeline_scroll_recovers_with_full_page_navigation(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                url = self.url("core:evidence:chronology", case_id=self.case.pk)

                def fail_fetch(route):
                    if route.request.resource_type == "fetch":
                        route.abort()
                    else:
                        route.continue_()

                page.route(f"{url}?**", fail_fetch)
                page.goto(f"{url}?view=day&day=2026-11-16")
                edge = page.locator('[data-timeline-edge="older"]')
                edge.scroll_into_view_if_needed()
                link = edge.get_by_role("link", name="Older dates", exact=True)
                expect(link).to_be_visible()
                expect(page.locator("[data-timeline-day]")).to_have_count(1)
                link.press("Enter")
                expect(page).to_have_url(re.compile(r"day=2026-11-15"))
                expect(
                    page.get_by_role("heading", name="Sunday, 15 November 2026")
                ).to_be_visible()

    def test_retry_all_failed_checks_with_keyboard_and_no_javascript(self):
        url = self.url("core:analysis:processing", case_id=self.case.pk)
        for engine in self.engines:
            prior, _ = start_run(
                self.owner,
                self.case.pk,
                "entities",
                [{"kind": "text", "id": str(self.note.pk)}],
                batch=self.batch,
            )
            AnalysisRun.objects.filter(pk=prior.pk).update(
                status=AnalysisRun.Status.FAILED
            )
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(url)
                expect(
                    page.get_by_role("link", name="Review failed checks")
                ).to_be_visible()
                expect(
                    page.get_by_role("button", name="Retry all failed checks")
                ).to_have_count(0)

            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                retry = page.get_by_role("button", name="Retry all failed checks")
                self.sign_in(page)
                page.goto(url + "?status=failed#processing-history")
                attention = page.get_by_role("region", name="Needs attention")
                self.capture_layouts(
                    page, engine, "retry-failed-checks", section=attention
                )
                self.assertGreaterEqual(retry.bounding_box()["height"], 44)
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                retry.focus()
                expect(retry).to_be_focused()
                self.capture(page, engine, "retry-failed-checks-dark-focus")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "retry-failed-checks-forced-colors")
                retry.press("Enter")
                expect(
                    page.get_by_text("Failed check retries requested.")
                ).to_be_visible()
                expect(
                    page.get_by_text("Retries are being queued", exact=True)
                ).to_be_visible()
                expect(retry).to_have_count(0)
                batch_id = parse_qs(urlsplit(page.url).query)["batch"][0]
                self.db(prepare_case_analysis_batch, UUID(batch_id))
                page.reload()
                expect(page.locator("#processing-history")).to_contain_text(
                    "1 reviewed · 1 queued"
                )
                expect(retry).to_have_count(0)
                retried = self.db(AnalysisRun.objects.get, previous_run=prior)
                self.assertEqual(retried.batch_id, self.batch.pk)

    def test_processing_analysis_run_breakdown_without_javascript(self):
        saved_runs = {
            "style_profiles": 7,
            "style_change": 3,
            "copied_text": 3,
            "text_topics": 5,
            "comm_patterns": 1,
        }
        AnalysisRun.objects.bulk_create(
            AnalysisRun(
                case=self.case,
                requested_by=self.owner,
                operation=operation,
                method_version=OPERATIONS[operation].version,
                inputs=[{"kind": "text-message", "evidence_id": str(self.message.pk)}],
                status=AnalysisRun.Status.COMPLETE,
            )
            for operation, count in saved_runs.items()
            for _ in range(count)
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.url("core:analysis:processing", case_id=self.case.pk))
                counts = page.locator("#processing-analysis-runs")
                expect(counts.get_by_text("21 saved runs", exact=True)).to_be_visible()
                expect(counts.locator("dt:visible")).to_have_count(0)
                disclosure = counts.locator("#processing-run-counts > summary")
                additional_method = counts.get_by_text(
                    "Inspect message timing", exact=True
                )
                expect(additional_method).to_be_hidden()
                disclosure.focus()
                expect(disclosure).to_be_focused()
                disclosure.press("Enter")
                expect(additional_method).to_be_visible()
                expect(counts.locator("dt:visible")).to_have_count(6)
                self.capture_layouts(
                    page, engine, "processing-analysis-runs", section=counts
                )
                disclosure.press("Space")
                expect(additional_method).to_be_hidden()
                page.set_viewport_size({"width": 390, "height": 844})
                counts.evaluate("element => element.scrollIntoView()")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "processing-analysis-runs-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "processing-analysis-runs-forced-colors")

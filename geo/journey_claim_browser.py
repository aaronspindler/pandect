"""Inspect a plan comparison from an exact quotation to the observed location."""

import re
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from core.analysis.worker import process_run
from geo.models import PersonLocationEvidence, VehicleLocationEvidence
from geo.trip_jobs import process_trips
from geo.trip_processing import prepare_case_trips, saved_trips
from people.models import Person
from text.models import TextEvidence
from vehicle.models import Vehicle


class JourneyClaimBrowserTests(BrowserTestCase):
    artifact_subdirectory = "plan-comparisons"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("plans-investigator")
        self.case = Case.objects.create(owner=self.owner, name="Harbour appointments")
        self.person = Person.objects.create(case=self.case, first_name="Jordan")
        self.partner = Person.objects.create(case=self.case, first_name="Morgan")
        self.quote = "Jordan and Morgan will meet at 42 Harbour Street."
        self.body = f"Earlier idea: {self.quote}\nJordan's proposal: {self.quote}"
        self.claim = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Appointment correspondence",
            body=self.body,
        )
        self.observation = PersonLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Harbour location report",
            person=self.person,
            address="42 Harbour St, Toronto",
            observed_at=datetime(2026, 11, 10, 14, tzinfo=UTC),
        )

    def capture_screen(self, page, engine, name):
        for width, height in (
            (1440, 900),
            (1440, 1024),
            (768, 1024),
            (390, 844),
            (320, 844),
        ):
            page.set_viewport_size({"width": width, "height": height})
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"),
                page.evaluate("document.documentElement.clientWidth") + 1,
                f"{name} at {width} × {height}",
            )
            page.evaluate("window.scrollTo(0, 0)")
            self.capture(page, engine, f"{name}-{width}x{height}")
        page.set_viewport_size({"width": 1440, "height": 900})

    def test_keyboard_comparison_and_citation_without_javascript(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                reader_url = (
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="text",
                        evidence_id=self.claim.pk,
                    )
                    + "?"
                    + urlencode({"q": "Appointment", "state": "all", "type": "text"})
                )
                page.goto(reader_url)
                page.get_by_text("Analyze evidence", exact=True).press("Enter")
                page.get_by_role("link", name="Choose analysis", exact=True).press(
                    "Enter"
                )
                choose = page.get_by_role(
                    "link", name="Compare plans with locations", exact=True
                )
                choose.press("Enter")
                self.capture_screen(page, engine, "choose")
                page.get_by_label("Find statement sources").fill("correspondence")
                page.get_by_role(
                    "region", name="Statements and time references"
                ).get_by_role("button", name="Search", exact=True).press("Enter")
                for title in (self.claim.title, self.observation.title):
                    checkbox = page.get_by_role("checkbox", name=title, exact=False)
                    checkbox.focus()
                    expect(checkbox).to_be_focused()
                    checkbox.press("Space")
                    expect(checkbox).to_be_checked()
                page.get_by_role("button", name="Choose passages").press("Enter")
                page.get_by_role("button", name="Restart source selection").press(
                    "Enter"
                )
                expect(page.get_by_label("Find statement sources")).to_have_value(
                    "correspondence"
                )
                expect(
                    page.get_by_role("checkbox", name=self.claim.title, exact=False)
                ).to_be_checked()
                page.get_by_role("button", name="Choose passages").press("Enter")
                page.get_by_label("Exact quotation", exact=True).fill(self.quote)
                page.get_by_label("Statement type").select_option("proposed")
                page.get_by_label("Claimed people or vehicles").select_option(
                    [f"person:{self.person.pk}", f"person:{self.partner.pk}"]
                )
                page.get_by_label("Place wording").fill("42 Harbour Street, Toronto")
                page.get_by_role(
                    "checkbox", name="This interpretation identifies"
                ).press("Space")
                page.get_by_label("Time basis").select_option("explicit")
                page.get_by_label("Start time").fill("2026-11-10 14:00+00:00")
                review = page.get_by_role("button", name="Review comparison")
                review.press("Enter")
                repeated = page.get_by_role("radio", name="Occurrence 2")
                repeated.press("Space")
                expect(repeated).to_be_checked()
                self.capture_screen(page, engine, "interpret")
                review.press("Enter")
                expect(
                    page.get_by_role("heading", name="Review comparison")
                ).to_be_visible()
                joint_names = re.compile(r"Jordan,\s*Morgan|Morgan,\s*Jordan")
                expect(
                    page.get_by_role("definition").filter(has_text=joint_names)
                ).to_be_visible()
                self.capture_screen(page, engine, "review")
                page.get_by_role("button", name="Queue comparison").press("Enter")
                expect(page.get_by_text("Queued", exact=True).first).to_be_visible()
                run_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                self.db(process_run, str(run_id))
                page.reload()
                passage = page.get_by_role(
                    "link", name="Read passage from Appointment correspondence"
                )
                expect(passage).to_be_visible()
                expect(
                    page.get_by_text(str(self.person), exact=True).first
                ).to_be_visible()
                expect(
                    page.get_by_text("42 Harbour St, Toronto", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("definition").filter(has_text=joint_names)
                ).to_be_visible()
                self.capture_screen(page, engine, "finding")
                expect(
                    page.get_by_role(
                        "link", name=self.observation.title, exact=True
                    ).first
                ).to_be_visible()
                passage.press("Enter")
                expect(page.locator("mark")).to_have_text(self.quote)
                self.assertIn(
                    "Jordan's proposal:",
                    page.locator("mark").locator("..").text_content(),
                )
                self.capture_screen(page, engine, "passage")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "passage-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "passage-forced-colors")
                page.emulate_media(forced_colors="none")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 391
                )
                self.capture(page, engine, "passage-large-text")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.evaluate("document.documentElement.style.zoom = '4'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"),
                    page.evaluate("document.documentElement.clientWidth") + 1,
                )
                self.capture(page, engine, "passage-zoom")
                page.evaluate("document.documentElement.style.zoom = ''")
                page.get_by_role("link", name="Back to comparison finding").press(
                    "Enter"
                )
                page.get_by_role("link", name="Back to evidence", exact=True).press(
                    "Enter"
                )
                expect(page).to_have_url(reader_url + "#evidence-reader")

    def test_cited_trip_observations_without_javascript(self):
        van = Vehicle.objects.create(case=self.case, model="Delivery van")
        for minute, place in [(3, "Harbour office"), (15, "Dock gate")]:
            VehicleLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                vehicle=van,
                title=place,
                address=place,
                observed_at=datetime(2026, 11, 10, 14, minute, tzinfo=UTC),
            )
        (generation,) = prepare_case_trips(self.owner, self.case.pk)
        for _ in range(3):
            process_trips(generation.pk, generation.token)
            generation.refresh_from_db()
            if generation.status == "complete":
                break
        self.assertEqual(generation.status, "complete", generation.error_code)
        trip = saved_trips(self.case).get()
        url = self.url("geo:trip_citation", case_id=self.case.pk, trip_id=trip.pk)
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(url)
                expect(
                    page.get_by_role("heading", name="Saved possible trip")
                ).to_be_visible()
                observation = page.get_by_role("link", name="Dock gate", exact=True)
                expect(observation).to_be_visible()
                self.capture_screen(page, engine, "cited-trip")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "cited-trip-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                observation.focus()
                expect(observation).to_be_focused()
                self.capture(page, engine, "cited-trip-focused")
                page.set_viewport_size({"width": 390, "height": 844})
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 391
                )
                observation.press("Enter")
                expect(page.get_by_role("heading", name="Dock gate")).to_be_visible()

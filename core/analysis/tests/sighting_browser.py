"""Case-scoped sighting proposal journeys through the server-rendered workflow."""

import re
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import urljoin
from uuid import UUID

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.services import start_run
from core.analysis.tests.test_openai_gateway import OPENAI
from core.analysis.tests.testing import external_grant_kwargs
from core.analysis.worker import process_run
from core.models import AnalysisRun
from geo.models import LocationEvidence, PersonLocationEvidence
from image.models import ImageEvidence
from image.tests.testing import plate_scene
from people.models import Person


class SightingBrowserTests(BrowserTestCase):
    artifact_subdirectory = "sighting-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("sightings-owner")
        self.editor = self.make_user("sightings-editor")
        self.viewer = self.make_user("sightings-viewer")
        self.case = Case.objects.create(owner=self.owner, name="Harbour photographs")
        for user, role in ((self.editor, "editor"), (self.viewer, "viewer")):
            CaseMembership.objects.create(case=self.case, user=user, role=role)

    def run_analysis(self, operation, kind, records, **approval):
        run, _ = start_run(
            self.owner,
            self.case.pk,
            operation,
            [{"kind": kind, "id": str(record.pk)} for record in records],
            **approval,
        )
        process_run(run.pk)
        run.refresh_from_db()
        self.assertIn(run.status, ("complete", "partial"), run.error_code)
        return run

    def open_anchor(self, page, title):
        page.goto(self.url("core:analysis:index", case_id=self.case.pk))
        page.get_by_role("link", name="Sighting groups", exact=True).press("Enter")
        page.get_by_role("link", name="Create sighting group").press("Enter")
        expect(
            page.get_by_role("heading", name="Choose an anchor observation")
        ).to_be_visible()
        anchor = page.get_by_role("link", name=re.compile(rf"^{re.escape(title)}\b"))
        anchor.focus()
        expect(anchor).to_be_focused()
        anchor.press("Enter")
        expect(
            page.get_by_role("group", name="Select direct connections")
        ).to_be_visible()

    def select_link(self, page, title):
        choice = page.get_by_role("checkbox", name=re.compile(re.escape(title))).first
        choice.focus()
        expect(choice).to_be_focused()
        choice.press("Space")
        expect(choice).to_be_checked()

    def publish_proposal(self, page):
        page.get_by_role("button", name="Save proposal").press("Enter")
        page.wait_for_url(re.compile(r"/analysis/[0-9a-f-]{36}/$"))
        expect(
            page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
        ).to_be_visible()
        run_id = UUID(page.url.rstrip("/").split("/")[-1])
        self.assertEqual(
            self.db(AnalysisRun.objects.get, pk=run_id).status,
            AnalysisRun.Status.QUEUED,
        )
        self.db(process_run, run_id)
        page.reload()
        expect(
            page.get_by_role("heading", name="Sighting group", exact=True)
        ).to_be_visible()
        return run_id

    def capture_layouts(self, page, engine, name):
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
            page.evaluate("window.scrollTo(0, 0)")
            self.capture(page, engine, f"{name}-{width}x{height}")
        page.evaluate("document.documentElement.style.fontSize = '200%'")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 321)
        self.capture(page, engine, f"{name}-large-text")
        page.evaluate("document.documentElement.style.fontSize = ''")
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        self.capture(page, engine, f"{name}-dark")
        page.emulate_media(color_scheme="light", forced_colors="active")
        self.capture(page, engine, f"{name}-forced-colors")
        page.emulate_media(forced_colors="none", reduced_motion="no-preference")

    def test_editor_reviews_originals_and_keeps_overlapping_proposal(self):
        person = Person.objects.create(case=self.case, first_name="Riley")
        anchor, linked, alternative = [
            PersonLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                person=person,
                title=title,
                address=address,
                observed_at=datetime(2026, 11, 16, hour, tzinfo=UTC),
            )
            for title, address, hour in (
                ("North entrance observation", "North entrance, Market Hall", 13),
                ("Ferry landing observation", "Ferry landing, Harbour Road", 14),
                ("Library entrance observation", "West entrance, Library Lane", 15),
            )
        ]
        contrary = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Closed entrance notice",
            address="Closed gate, Market Hall",
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.editor)
                self.open_anchor(page, anchor.title)
                page.get_by_role("button", name="Save proposal").press("Enter")
                expect(
                    page.get_by_role("alert").filter(has_text="Check the details below")
                ).to_be_visible()
                page.get_by_text("Available comparisons", exact=True).press("Enter")
                expect(
                    page.get_by_text(
                        "image-similarity comparisons are unavailable.",
                        exact=False,
                    )
                ).to_be_visible()
                self.select_link(page, linked.title)
                page.get_by_text(
                    "Add supporting or contrary records", exact=True
                ).press("Enter")
                context = page.get_by_role("group", name=contrary.title, exact=True)
                context.get_by_role("radio", name="Contrary", exact=True).press("Space")
                self.capture_layouts(page, engine, "create-proposal")
                original_run = self.publish_proposal(page)
                expect(
                    page.get_by_text("Explicit source association", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name=contrary.title, exact=True)
                ).to_be_visible()
                self.capture_layouts(page, engine, "proposed-group")

                source = page.get_by_role("link", name=linked.title, exact=True).first
                source.focus()
                expect(source).to_be_focused()
                source.press("Enter")
                expect(page).to_have_url(
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="person-location",
                        evidence_id=linked.pk,
                    )
                )
                page.go_back()
                review = page.get_by_role(
                    "button", name="Accept for review", exact=True
                )
                review.focus()
                expect(review).to_be_focused()
                self.assertNotEqual(
                    review.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                self.capture(page, engine, "review-keyboard-mobile")
                review.press("Enter")
                expect(
                    page.get_by_text("Accepted for review", exact=True).first
                ).to_be_visible()

                self.open_anchor(page, anchor.title)
                self.select_link(page, alternative.title)
                alternative_run = self.publish_proposal(page)
                expect(
                    page.get_by_role("heading", name="Multiple groupings")
                ).to_be_visible()
                original_path = reverse(
                    "core:analysis:sighting_detail",
                    kwargs={"case_id": self.case.pk, "run_id": original_run},
                )
                original_group = page.locator(f'a[href="{original_path}"]')
                expect(original_group).to_contain_text(linked.title)
                original_group.press("Enter")
                expect(
                    page.get_by_text("Accepted for review", exact=True).first
                ).to_be_visible()
                self.assertNotEqual(original_run, alternative_run)

            with (
                self.subTest(engine=engine, role="viewer"),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.live_server_url + original_path)
                expect(
                    page.get_by_role("button", name="Accept for review")
                ).to_have_count(0)
                expect(
                    page.get_by_text("Accepted for review", exact=True).first
                ).to_be_visible()
                page.goto(
                    self.url("core:analysis:sighting_groups", case_id=self.case.pk)
                )
                expect(
                    page.get_by_role("link", name="Create sighting group")
                ).to_have_count(0)

    @override_settings(PANDECT_AI_GATEWAY=OPENAI)
    def test_plate_region_and_spatial_comparison_links_open_retained_sources(self):
        approval = external_grant_kwargs(self.case, "plate_ocr")
        images = [
            ImageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=title,
                file=ContentFile(
                    plate_scene(angle=angle), name=f"synthetic-{angle}.jpeg"
                ),
            )
            for title, angle in (("Market photograph", 0), ("Harbour photograph", 8))
        ]
        with patch(
            "core.analysis.gateway.GatewayClient.read_image",
            return_value=("TEST 482", False),
        ) as recognize:
            for image in images:
                self.run_analysis("plate_ocr", "image", [image], **approval)
            self.assertGreaterEqual(recognize.call_count, len(images))
        locations = [
            LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=title,
                latitude=latitude,
                longitude="-79.380000",
                observed_at=datetime(2026, 11, 16, 13, minute, tzinfo=UTC),
            )
            for title, latitude, minute in (
                ("Market entrance reading", "43.650000", 0),
                ("Nearby florist reading", "43.650200", 1),
            )
        ]
        self.run_analysis("spatial_groups", "location", locations)
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.set_viewport_size({"width": 390, "height": 844})
                self.open_anchor(page, images[0].title)
                self.select_link(page, images[1].title)
                self.publish_proposal(page)
                expect(
                    page.get_by_text("Shared identifier", exact=True)
                ).to_be_visible()
                page.get_by_role(
                    "link", name=re.compile(r"TEST\s*482", re.I)
                ).first.press("Enter")
                region = page.get_by_role(
                    "img", name="Source image with the possible plate region marked"
                )
                region.scroll_into_view_if_needed()
                expect(region.locator("rect")).to_be_visible()
                page.wait_for_load_state("load")
                preview_url = urljoin(
                    page.url, region.locator("image").get_attribute("href")
                )
                response = page.request.get(preview_url)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["content-type"], "image/jpeg")
                self.assertIn("no-store", response.headers["cache-control"])
                self.capture(page, engine, "plate-original-region-mobile")

                self.open_anchor(page, locations[0].title)
                self.select_link(page, locations[1].title)
                self.publish_proposal(page)
                expect(page.get_by_text("Place proximity", exact=True)).to_be_visible()
                self.capture(page, engine, "spatial-proposal-mobile")
                proposal_url = page.url
                location_link = page.get_by_role(
                    "link", name=locations[1].title, exact=True
                )
                location_url = urljoin(
                    proposal_url, location_link.get_attribute("href")
                )
                location_link.press("Enter")
                expect(page).to_have_url(location_url)
                self.open_evidence_panel(page, "more").get_by_role(
                    "button", name="Archive", exact=True
                ).press("Enter")
                try:
                    expect(
                        page.get_by_role("status").filter(
                            has_text="Evidence archived. Its original is retained."
                        )
                    ).to_be_visible()
                    page.goto(proposal_url)
                    expect(
                        page.get_by_role("button", name="Accept for review")
                    ).to_have_count(0)
                    expect(
                        page.get_by_text("Proposal unavailable.", exact=False)
                    ).to_be_visible()
                    expect(
                        page.get_by_role(
                            "link", name=locations[0].title, exact=True
                        ).first
                    ).to_have_count(0)
                    self.capture(page, engine, "archived-proposal-mobile")
                finally:
                    page.goto(location_url)
                    self.open_evidence_panel(page, "more").get_by_role(
                        "button", name="Restore", exact=True
                    ).press("Enter")
                    expect(
                        page.get_by_role("status").filter(
                            has_text="Evidence restored to the active collection."
                        )
                    ).to_be_visible()
                page.goto(proposal_url)
                expect(
                    page.get_by_role("button", name="Accept for review", exact=True)
                ).to_be_visible()

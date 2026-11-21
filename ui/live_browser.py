"""Explicit browser journeys for progress fragments, using isolated PostgreSQL."""

from datetime import UTC, datetime, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.operations import OPERATIONS
from core.analysis.services import process_run, start_run
from core.models import AnalysisRun
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence, VehicleLocationImport
from geo.testing import csv_content
from geo.trip_jobs import process_trips
from geo.trip_processing import prepare_case_trips
from geo.vehicle_import_jobs import enqueue_preparation, process_import
from phone.tests.test_imessage_views import message_csv
from text.models import TextEvidence
from vehicle.models import Vehicle


class LiveUpdatesBrowserTests(BrowserTestCase):
    artifact_subdirectory = "live-updates"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("live-owner")
        self.viewer = self.make_user("live-viewer")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic live case")
        CaseMembership.objects.create(case=self.case, user=self.viewer, role="viewer")
        self.source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic live source",
            body="Contact live@example.test about the proposed meeting.",
        )

    def queue(self):
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "entities",
            [{"kind": "text", "id": str(self.source.pk)}],
        )
        return run.pk

    def test_results_and_findings_update_without_navigation(self):
        for engine in self.engines:
            for name in ("core:analysis:detail", "core:evidence:detail"):
                run_id = self.queue()
                with (
                    self.subTest(engine=engine, screen=name),
                    self.browser_page(engine) as (_, page),
                ):
                    self.sign_in(page)
                    kwargs = {"case_id": self.case.pk}
                    if name == "core:analysis:detail":
                        kwargs["run_id"] = run_id
                        region = page.locator("#analysis-result")
                    else:
                        kwargs |= {"kind": "text", "evidence_id": self.source.pk}
                        region = page.locator("#evidence-findings")
                    page.goto(self.url(name, **kwargs) + "?state=all")
                    page.evaluate("window.liveSentinel = 42")
                    expect(region).to_have_attribute("data-live-poll", "true")
                    self.db(process_run, run_id)
                    expect(region).to_have_attribute(
                        "data-live-poll", "false", timeout=15000
                    )
                    expect(region).to_contain_text("live@example.test")
                    self.assertEqual(page.evaluate("window.liveSentinel"), 42)

                    self.assertTrue(page.url.endswith("?state=all"))
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
                            width + 1,
                        )
                        self.capture(page, engine, f"{name.rsplit(':', 2)[1]}-{width}")
                    page.evaluate("document.documentElement.style.fontSize = '200%'")
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), 321
                    )
                    self.capture(page, engine, "live-large-text")
                    page.evaluate("document.documentElement.style.fontSize = ''")
                    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                    self.capture(page, engine, "live-dark")
                    page.emulate_media(color_scheme="light", forced_colors="active")
                    self.capture(page, engine, "live-forced-colors")
                    if name == "core:evidence:detail":
                        more = self.open_evidence_panel(page, "more")
                        rerun = more.get_by_role(
                            "button", name="Run checks", exact=True
                        )
                        expect(rerun).to_be_visible()
                        self.assertGreaterEqual(rerun.bounding_box()["height"], 44)
                        self.capture(page, engine, "live-rerun-action")

    @override_settings(PANDECT_MAP_TILE_URL="")
    def test_trip_summaries_update_and_keep_the_map_in_place(self):
        def prepare():
            vehicle = Vehicle.objects.create(case=self.case, model="Synthetic live van")
            for index in range(2):
                VehicleLocationEvidence.objects.create(
                    case=self.case,
                    uploaded_by=self.owner,
                    title=f"Synthetic position {index}",
                    vehicle=vehicle,
                    latitude=43,
                    longitude=-79 + index,
                    observed_at=datetime(2026, 11, 1, 12, tzinfo=UTC)
                    + timedelta(minutes=10 * index),
                )
            generation = prepare_case_trips(
                self.owner, self.case.pk, vehicle_ids=[vehicle.pk]
            )[0]
            return vehicle, generation

        def finish(generation):
            for _ in range(10):
                process_trips(str(generation.pk), str(generation.token))
                generation.refresh_from_db()
                if generation.status == "complete":
                    return
            self.fail("Synthetic trip generation did not finish")

        for engine in self.engines:
            for screen in ("map", "vehicle", "report"):
                vehicle, generation = prepare()
                with (
                    self.subTest(engine=engine, screen=screen),
                    self.browser_page(engine) as (_, page),
                ):
                    self.sign_in(page)
                    if screen == "map":
                        url = self.url("geo:case_map", case_id=self.case.pk)
                        region_id = "map-progress"
                    elif screen == "vehicle":
                        url = self.url(
                            "vehicle:detail",
                            case_id=self.case.pk,
                            vehicle_id=vehicle.pk,
                        )
                        region_id = "vehicle-trips"
                    else:
                        url = (
                            self.live_server_url
                            + reverse(
                                "core:analysis:report",
                                args=[self.case.pk, "vehicle-trips"],
                            )
                            + f"?vehicle_id={vehicle.pk}"
                        )
                        region_id = "report-results"
                    page.goto(url)
                    page.evaluate(
                        "window.liveSentinel = 42; "
                        "window.liveMap = document.querySelector('[data-case-map]')"
                    )
                    self.db(finish, generation)
                    expect(page.locator(f"#{region_id}")).to_have_attribute(
                        "data-live-poll", "false", timeout=15000
                    )
                    if screen == "map":
                        expect(page.locator(".trip-list")).to_contain_text(
                            "Synthetic live van"
                        )
                    else:
                        expect(
                            page.get_by_role(
                                "link", name="Inspect trip · 2 observations"
                            ).first
                        ).to_be_visible()
                    self.assertEqual(page.evaluate("window.liveSentinel"), 42)
                    if screen == "map":
                        self.assertTrue(
                            page.evaluate(
                                "window.liveMap === "
                                "document.querySelector('[data-case-map]')"
                            )
                        )

    def test_processing_keeps_filters_focus_and_open_disclosures(self):
        AnalysisRun.objects.bulk_create(
            AnalysisRun(
                case=self.case,
                requested_by=self.owner,
                operation=operation,
                method_version=OPERATIONS[operation].version,
                inputs=[{"kind": "text", "evidence_id": str(self.source.pk)}],
                status=AnalysisRun.Status.COMPLETE,
            )
            for operation in (
                "entities",
                "style_profiles",
                "style_change",
                "copied_text",
                "text_topics",
                "content_events",
            )
        )
        for saved_runs, engine in enumerate(self.engines, start=6):
            run_id = self.queue()
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(
                    self.url("core:analysis:processing", case_id=self.case.pk)
                    + "?status=queued"
                )
                counts = page.locator("#processing-analysis-runs")
                expect(
                    counts.get_by_text(f"{saved_runs} saved runs", exact=True)
                ).to_be_visible()
                breakdown = counts.locator("#processing-run-counts")
                breakdown.locator("summary").press("Enter")
                expect(breakdown).to_have_attribute("open", "")
                filters = page.locator("#processing-history details").first
                filters.evaluate("element => element.open = true")
                field = page.get_by_label("Status", exact=True)
                field.select_option("complete")
                field.focus()
                self.db(
                    AnalysisRun.objects.filter(pk=run_id).update,
                    status="running",
                    progress=45,
                )
                expect(page.locator("#processing-activity")).to_contain_text(
                    "45%", timeout=15000
                )
                expect(field).to_be_focused()
                expect(field).to_have_value("complete")
                expect(filters).to_have_attribute("open", "")
                self.db(AnalysisRun.objects.filter(pk=run_id).update, status="queued")
                self.db(process_run, run_id)
                expect(page.locator("#processing-activity")).to_have_attribute(
                    "data-live-poll", "false", timeout=15000
                )
                expect(
                    counts.get_by_text(f"{saved_runs + 1} saved runs", exact=True)
                ).to_be_visible()
                expect(breakdown).to_have_attribute("open", "")
                expect(field).to_be_focused()
                expect(field).to_have_value("complete")

    def test_hidden_tabs_failures_manual_retry_and_revocation(self):
        for engine in self.engines:
            run_id = self.queue()
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(
                    self.url(
                        "core:analysis:detail", case_id=self.case.pk, run_id=run_id
                    )
                )
                requests = []
                page.on(
                    "request",
                    lambda request, requests=requests: (
                        requests.append(request)
                        if request.headers.get("x-pandect-live")
                        else None
                    ),
                )
                page.evaluate(
                    "Object.defineProperty(document, 'hidden', "
                    "{configurable: true, value: true}); "
                    "document.dispatchEvent(new Event('visibilitychange'))"
                )
                page.clock.install()
                page.clock.run_for(6000)
                self.assertEqual(requests, [])
                page.route(
                    "**/analysis/*/",
                    lambda route: (
                        route.fulfill(status=503)
                        if route.request.headers.get("x-pandect-live")
                        else route.continue_()
                    ),
                )
                page.evaluate(
                    "Object.defineProperty(document, 'hidden', "
                    "{configurable: true, value: false}); "
                    "document.dispatchEvent(new Event('visibilitychange'))"
                )
                expect(page.locator("[data-live-message]")).to_contain_text(
                    "Retrying shortly"
                )
                for milliseconds in (11000, 21000):
                    with page.expect_response(
                        lambda response: (
                            response.status == 503
                            and response.request.headers.get("x-pandect-live") == "1"
                        )
                    ) as retry:
                        page.clock.run_for(milliseconds)
                    retry.value.finished()
                expect(page.locator("[data-live-message]")).to_contain_text(
                    "Automatic updates paused"
                )
                page.unroute("**/analysis/*/")
                self.db(process_run, run_id)
                page.get_by_role("link", name="Refresh status", exact=True).click()
                expect(page.locator("#analysis-result")).to_have_attribute(
                    "data-live-poll", "false"
                )
                self.db(
                    CaseMembership.objects.filter(
                        case=self.case, user=self.viewer
                    ).delete
                )
                page.get_by_role("link", name="Refresh status", exact=True).click()
                expect(page.locator("[data-live-message]")).to_contain_text(
                    "access may have changed"
                )
                expect(
                    page.get_by_role("link", name="All cases", exact=True).last
                ).to_be_visible()
                count = len(requests)
                page.clock.run_for(30000)
                self.assertEqual(len(requests), count)
                self.db(
                    CaseMembership.objects.create,
                    case=self.case,
                    user=self.viewer,
                    role="viewer",
                )

    def test_import_previews_become_review_forms_in_place(self):
        def vehicle_preview():
            document = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title="Synthetic locations",
                file=SimpleUploadedFile("locations.csv", csv_content(), "text/csv"),
            )
            batch = VehicleLocationImport.objects.create(
                case=self.case,
                document=document,
                uploaded_by=self.owner,
                timezone="UTC",
            )
            enqueue_preparation(self.owner, self.case.pk, batch.pk)
            batch.refresh_from_db()
            return batch

        def message_preview():
            document = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title="Synthetic messages",
                file=SimpleUploadedFile("messages.csv", message_csv(), "text/csv"),
            )
            run, _ = start_run(
                self.owner,
                self.case.pk,
                "imessage_import",
                [{"kind": "document", "id": str(document.pk)}],
                parameters={"phase": "preview", "timezone": "UTC"},
            )
            return run

        for engine in self.engines:
            for kind in ("vehicle", "message"):
                record = vehicle_preview() if kind == "vehicle" else message_preview()
                with (
                    self.subTest(engine=engine, kind=kind),
                    self.browser_page(engine) as (_, page),
                ):
                    self.sign_in(page)
                    if kind == "vehicle":
                        url = self.url(
                            "geo:vehicle_import_review",
                            case_id=self.case.pk,
                            import_id=record.pk,
                        )
                    else:
                        url = self.url(
                            "phone:imessage_review",
                            case_id=self.case.pk,
                            run_id=record.pk,
                        )
                    page.goto(url)
                    page.evaluate("window.liveSentinel = 42")
                    if kind == "vehicle":
                        self.db(process_import, str(record.pk), str(record.job_token))
                        expect(
                            page.get_by_role(
                                "heading", name="Choose the vehicles", exact=True
                            )
                        ).to_be_visible(timeout=15000)
                    else:
                        self.db(process_run, record.pk)
                        expect(
                            page.get_by_role(
                                "link", name="Review prepared messages", exact=True
                            )
                        ).to_be_visible(timeout=15000)
                    expect(page.locator("[data-live-region]")).to_have_attribute(
                        "data-live-poll", "false"
                    )
                    self.assertEqual(page.evaluate("window.liveSentinel"), 42)
                    if kind == "message":
                        page.get_by_role(
                            "link", name="Review prepared messages", exact=True
                        ).click()
                        expect(
                            page.get_by_role(
                                "button", name="Import messages", exact=True
                            )
                        ).to_be_visible()

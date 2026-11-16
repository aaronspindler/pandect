"""Synthetic saved-trip map journeys with mocked tile and geocoder responses."""

import io
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.paginator import Paginator
from django.db import connections
from django.test import override_settings
from django.urls import reverse
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

from cases.models import Case
from core.analysis.services import process_run, start_run
from core.models import AnalysisReview
from geo.analysis.external.geocoding import Geocoder
from geo.models import LocationEvidence, VehicleLocationEvidence
from geo.testing import LOCAL_GEOCODER
from geo.trip_jobs import process_trips
from geo.trip_processing import prepare_case_trips, saved_trips
from people.models import Person
from vehicle.models import Vehicle


@override_settings(
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    PANDECT_MAP_TILE_URL="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    PANDECT_MAP_TILE_ORIGIN="https://tile.openstreetmap.org",
)
class InvestigationMapBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="map-investigator", email="map@example.test"
        )
        EmailAddress.objects.create(
            user=self.owner, email=self.owner.email, verified=True, primary=True
        )
        self.case = Case.objects.create(
            owner=self.owner, name="Synthetic harbour investigation"
        )
        self.person = Person.objects.create(case=self.case, first_name="Alex Example")
        self.vehicle = Vehicle.objects.create(case=self.case, make="Synthetic van")
        records = []
        for index in range(505):
            item = VehicleLocationEvidence(
                case=self.case,
                uploaded_by=self.owner,
                vehicle=self.vehicle,
                title=f"Synthetic trip source {index}",
                address=f"{index} Example Street",
                latitude="43.64",
                longitude=Decimal("-79.38") + Decimal(index) / 100000,
                observed_at=datetime(2026, 11, 1, 12, tzinfo=UTC)
                + timedelta(seconds=index),
                speed_kmh=10,
            )
            item._prepare_original()
            records.append(item)
        VehicleLocationEvidence.objects.bulk_create(records)
        LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Raw location excluded from trip map",
            latitude=1,
            longitude=1,
        )
        self.build_trips()
        self.trip = saved_trips(self.case).get()
        self.client.force_login(self.owner)
        self.cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.map_url = self.live_server_url + reverse(
            "geo:case_map", args=(self.case.pk,)
        )
        self.detail_url = self.live_server_url + reverse(
            "geo:trip_detail", args=(self.case.pk, self.trip.pk)
        )
        self.artifacts = Path("data/validation/maps").resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        image = Image.new("RGB", (256, 256), "#edf0e4")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 150, 256, 256), fill="#d4e5ec")
        draw.line((0, 80, 256, 180), fill="white", width=9)
        image.save(buffer, "PNG")
        self.tile = buffer.getvalue()

    def build_trips(self, *, vehicle_ids=None):
        generation = prepare_case_trips(
            self.owner, self.case.pk, vehicle_ids=vehicle_ids
        )[0]
        for _ in range(10):
            if generation.status == "complete":
                return
            process_trips(str(generation.pk), str(generation.token))
            generation.refresh_from_db()
        self.fail(f"Synthetic trip processing did not complete: {generation.status}")

    def engines(self):
        return os.environ.get("PANDECT_BROWSER_ENGINES", "chromium,webkit").split(",")

    def authenticate(self, context):
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.cookie,
                    "url": self.live_server_url,
                }
            ]
        )

    def capture(self, page, engine, name, *, wait_for_trip=True):
        page.evaluate("window.scrollTo(0, 0)")
        if wait_for_trip and page.locator(".trip-path-canvas").count():
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            expect(page.locator("[data-case-map]")).to_have_attribute(
                "data-trip-complete", "true"
            )
        page.screenshot(
            path=str(self.artifacts / f"{engine}-{name}.png"),
            full_page=True,
            caret="initial",
        )

    def in_database(self, operation):
        def run():
            try:
                return operation()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(run).result()

    def mock_tiles(self, context, requests):
        def serve(route):
            if route.request.url.startswith(self.live_server_url + "/"):
                route.continue_()
            else:
                requests.append(route.request)
                route.fulfill(status=200, content_type="image/png", body=self.tile)

        context.route("**/*", serve)

    def test_poi_marker_content_is_text_even_when_it_contains_markup(self):
        markup = '<img src=x onerror="window.pandectInjected=true">'
        points = [
            {
                "number": markup,
                "title": markup,
                "latitude": 43.64,
                "longitude": -79.38,
                "url": "/",
            }
        ]
        unsafe_urls = (
            "javascript:window.pandectInjected=true",
            "data:text/html,<script>window.pandectInjected=true</script>",
            "https://unrelated.example.test/",
            "http://[invalid",
        )
        points.extend(
            {
                "number": f"unsafe-{index}",
                "title": "Unsafe source link",
                "latitude": 43.64 + (index + 1) / 100,
                "longitude": -79.38,
                "url": url,
            }
            for index, url in enumerate(unsafe_urls)
        )
        with (
            patch(
                "geo.map_views.poi_map_data",
                return_value=(Paginator([], 100).page(1), points),
            ),
            sync_playwright() as playwright,
        ):
            for engine in self.engines():
                with self.subTest(engine=engine):
                    browser = getattr(playwright, engine).launch()
                    context = browser.new_context()
                    self.authenticate(context)
                    self.mock_tiles(context, [])
                    page = context.new_page()
                    page.goto(self.map_url)
                    marker = page.locator(".case-map-poi").first
                    expect(marker.locator("b")).to_have_text(markup)
                    expect(marker.locator("img")).to_have_count(0)
                    marker.click()
                    popup = page.locator(".leaflet-popup-content")
                    expect(popup.locator("a")).to_have_text(f"{markup}. {markup}")
                    expect(popup.locator("img")).to_have_count(0)
                    for index in range(len(unsafe_urls)):
                        page.locator(".case-map-poi").nth(index + 1).dispatch_event(
                            "click"
                        )
                        expect(popup.locator("a")).not_to_have_attribute("href")
                    self.assertIsNone(page.evaluate("window.pandectInjected"))
                    browser.close()

    def test_complete_trip_keyboard_sources_and_responsive_layouts(self):
        with sync_playwright() as playwright:
            for engine in self.engines():
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
                )
                self.authenticate(context)
                requests = []
                self.mock_tiles(context, requests)
                page = context.new_page()
                errors = []
                page.on(
                    "pageerror", lambda error, errors=errors: errors.append(str(error))
                )
                page.goto(self.map_url)
                expect(page.locator("[data-case-map]")).to_have_attribute(
                    "data-trip-complete", "true"
                )
                expect(page.locator("[data-case-map]")).to_have_attribute(
                    "data-trip-sources", "505"
                )
                expect(page.locator(".trip-path-canvas")).to_have_count(1)
                expect(
                    page.get_by_text("Raw location excluded from trip map")
                ).to_have_count(0)
                expect(page.locator("[data-trip-status]")).to_contain_text("505 of 505")
                show = page.get_by_role("button", name="Show selected trip and POIs")
                show.focus()
                page.keyboard.press("Enter")
                canvas = page.locator("#investigation-map")
                canvas.focus()
                page.keyboard.press("ArrowRight")
                page.keyboard.press("+")
                expect(page.locator("[data-case-map]")).to_have_attribute(
                    "data-trip-complete", "true"
                )
                self.assertTrue(
                    all(
                        re.fullmatch(
                            r"https://tile\.openstreetmap\.org/\d+/\d+/\d+\.png",
                            request.url,
                        )
                        and request.method == "GET"
                        and request.post_data is None
                        for request in requests
                    )
                )
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    expect(page.locator("[data-case-map]")).to_have_attribute(
                        "data-trip-complete", "true"
                    )
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width
                    )
                    self.capture(page, engine, f"trip-map-{width}x{height}")
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "trip-map-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "trip-map-forced-colors")
                    page.emulate_media(forced_colors="none")
                page.get_by_role(
                    "link", name="Show trip and sources", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Trip observations", exact=True)
                ).to_be_visible()
                expect(page.locator(".map-location-list > li")).to_have_count(50)
                page.get_by_role("link", name="Next observations").click()
                expect(page.locator(".map-location-list > li").first).to_contain_text(
                    "50 Example Street"
                )
                page.locator(".map-location-list a").first.focus()
                page.keyboard.press("Enter")
                expect(
                    page.get_by_role(
                        "heading", name="Synthetic trip source 50", exact=True
                    )
                ).to_be_visible()
                self.assertEqual(errors, [])
                context.close()
                browser.close()

    def test_fallback_filters_tiles_and_geometry_failure(self):
        with sync_playwright() as playwright:
            for engine in self.engines():
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(
                    java_script_enabled=False, viewport={"width": 390, "height": 844}
                )
                self.authenticate(context)
                page = context.new_page()
                page.goto(self.map_url)
                page.locator(".map-search > summary").click()
                page.get_by_label("Vehicle", exact=True).select_option(
                    str(self.vehicle.pk)
                )
                page.get_by_role("button", name="Apply filters").click()
                page.get_by_role(
                    "link", name="Show trip and sources", exact=True
                ).click()
                expect(
                    page.get_by_role("link", name="0 Example Street", exact=True)
                ).to_be_visible()
                self.capture(page, engine, "trip-no-js")
                context.close()
                context = browser.new_context(viewport={"width": 390, "height": 844})
                self.authenticate(context)
                self.mock_tiles(context, [])
                context.route(
                    "**/map/trips/**",
                    lambda route: (
                        route.fulfill(status=503, body="unavailable")
                        if route.request.headers.get("accept") == "application/json"
                        else route.continue_()
                    ),
                )
                page = context.new_page()
                page.goto(self.map_url)
                expect(page.locator("[data-trip-status]")).to_contain_text(
                    "could not load"
                )
                expect(
                    page.get_by_role("link", name="Show trip and sources")
                ).to_be_visible()
                self.capture(page, engine, "trip-geometry-failure", wait_for_trip=False)
                context.close()
                browser.close()

    def test_person_pois_from_map_and_without_javascript(self):
        with sync_playwright() as playwright:
            for engine in self.engines():
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
                )
                self.authenticate(context)

                def serve(route):
                    if route.request.url.startswith(self.live_server_url + "/"):
                        route.continue_()
                    else:
                        route.fulfill(
                            status=200, content_type="image/png", body=self.tile
                        )

                context.route("**/*", serve)
                page = context.new_page()
                errors = []
                page.on(
                    "pageerror", lambda error, errors=errors: errors.append(str(error))
                )
                page.goto(self.map_url)
                canvas = page.locator("#investigation-map")
                canvas.click(position={"x": 100, "y": 100})
                page.get_by_role("link", name="Add POI here", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Add POI", exact=True)
                ).to_be_visible()
                self.assertTrue(page.get_by_label("Latitude", exact=True).input_value())
                self.assertTrue(
                    page.get_by_label("Longitude", exact=True).input_value()
                )
                page.get_by_label("Person", exact=True).select_option(
                    str(self.person.pk)
                )
                page.get_by_label("Latitude", exact=True).fill("43.642")
                page.get_by_label("Longitude", exact=True).fill("-79.382")
                page.get_by_label("Source / citation", exact=True).fill(
                    "Synthetic POI source"
                )
                page.get_by_role("button", name="Add POI", exact=True).focus()
                page.keyboard.press("Enter")
                expect(
                    page.get_by_role("heading", name="Home · Alex Example", exact=True)
                ).to_be_visible()
                expect(page.locator(".message[role=status]")).to_contain_text(
                    "POI added"
                )
                page.get_by_role("link", name="Edit POI", exact=True).click()
                page.get_by_label("POI type", exact=True).select_option("custom")
                page.get_by_label("Custom label", exact=True).fill("")
                page.get_by_role("button", name="Save changes", exact=True).click()
                expect(page.locator("[data-error-summary]")).to_be_focused()
                expect(
                    page.get_by_label("Source / citation", exact=True)
                ).to_have_value("Synthetic POI source")
                page.get_by_label("Custom label", exact=True).fill(
                    f"Meeting place {engine}"
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
                        page.evaluate("document.documentElement.scrollWidth"), width
                    )
                    self.capture(page, engine, f"poi-form-{width}x{height}")
                page.get_by_role("button", name="Save changes", exact=True).click()
                label = f"Meeting place {engine} · Alex Example"
                expect(
                    page.get_by_role("heading", name=label, exact=True)
                ).to_be_visible()
                page.get_by_role("link", name="Back to map", exact=True).click()
                page.wait_for_url(self.map_url, wait_until="domcontentloaded")
                page.bring_to_front()
                show = page.get_by_role(
                    "button", name=f"Show {label} on map", exact=True
                )
                show.scroll_into_view_if_needed()
                show.press("Enter")
                expect(canvas).to_be_focused()
                expect(page.locator(".leaflet-popup-content")).to_contain_text("POI")
                expect(page.locator(".leaflet-popup-content")).to_contain_text(label)
                self.assertGreaterEqual(page.locator(".case-map-poi").count(), 1)
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                    (844, 390),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width
                    )
                    page.wait_for_function("""() => {
                        const popup = document.querySelector('.leaflet-popup')
                          .getBoundingClientRect();
                        const zoom = document.querySelector('.leaflet-control-zoom')
                          .getBoundingClientRect();
                        return popup.top >= zoom.bottom ||
                          popup.left >= zoom.right || popup.right <= zoom.left;
                    }""")
                    self.capture(page, engine, f"poi-map-{width}x{height}")
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "poi-map-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "poi-map-forced-colors")
                    page.emulate_media(forced_colors="none")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 844
                )
                self.capture(page, engine, "poi-map-enlarged-text")
                self.assertEqual(errors, [])
                context.close()
                # Add and delete also work without the map library or JavaScript.
                context = browser.new_context(
                    java_script_enabled=False, viewport={"width": 390, "height": 844}
                )
                self.authenticate(context)
                page = context.new_page()
                person_url = self.live_server_url + reverse(
                    "people:detail", args=(self.case.pk, self.person.pk)
                )
                page.goto(person_url)
                page.get_by_role("link", name="Add POI", exact=True).click()
                expect(page.get_by_label("Person", exact=True)).to_have_value(
                    str(self.person.pk)
                )
                page.get_by_label("POI type", exact=True).select_option("work")
                page.get_by_label("Latitude", exact=True).fill("43.65")
                page.get_by_label("Longitude", exact=True).fill("-79.39")
                page.get_by_role("button", name="Add POI", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Work · Alex Example", exact=True)
                ).to_be_visible()
                self.capture(page, engine, "poi-detail-no-js")
                page.locator("summary").filter(has_text="Record details").focus()
                page.locator("summary").filter(has_text="Record details").press("Enter")
                page.get_by_role("link", name="Delete POI", exact=True).click()
                page.get_by_label(
                    "I understand this permanently deletes this POI.", exact=True
                ).check()
                page.get_by_role(
                    "button", name="Permanently delete POI", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Alex Example", exact=True)
                ).to_be_visible()
                expect(page.locator(".message[role=status]")).to_contain_text(
                    "POI deleted"
                )
                context.close()
                browser.close()

    @override_settings(**LOCAL_GEOCODER)
    def test_address_trip_becomes_mappable_through_existing_finding_review(self):
        vehicle = Vehicle.objects.create(case=self.case, make="Synthetic address van")
        records = [
            VehicleLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                vehicle=vehicle,
                title=f"Address source {index}",
                address=f"{index + 10} Synthetic Avenue",
                observed_at=datetime(2026, 11, 2, 12, index, tzinfo=UTC),
            )
            for index in range(2)
        ]
        self.build_trips(vehicle_ids=[vehicle.pk])
        trip = saved_trips(self.case, vehicle_id=vehicle.pk).get()
        trip_url = self.live_server_url + reverse(
            "geo:trip_detail", args=(self.case.pk, trip.pk)
        )
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "geocode_locations",
            [{"kind": "vehicle-location", "id": str(item.pk)} for item in records],
            parameters={"reverse": False},
        )
        with patch.object(
            Geocoder,
            "lookup",
            side_effect=[
                [
                    {
                        "latitude": 43.65,
                        "longitude": -79.39,
                        "address": "Synthetic first estimate",
                        "reference_id": "way:1",
                    }
                ],
                [
                    {
                        "latitude": 43.66,
                        "longitude": -79.40,
                        "address": "Synthetic last estimate",
                        "reference_id": "way:2",
                    }
                ],
            ],
        ):
            process_run(str(run.pk))
        with sync_playwright() as playwright:
            for engine in self.engines():
                self.in_database(
                    lambda: AnalysisReview.objects.create(
                        case=self.case,
                        result=run.result,
                        reviewed_by=self.owner,
                        item_index=0,
                        decision="unresolved",
                    )
                )
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(viewport={"width": 390, "height": 844})
                self.authenticate(context)
                self.mock_tiles(context, [])
                page = context.new_page()
                page.goto(trip_url)
                expect(page.locator("[data-trip-status]")).to_contain_text(
                    "Review address estimates"
                )
                expect(page.locator(".case-map-endpoint")).to_have_count(0)
                page.get_by_role(
                    "link", name="Review address lookup", exact=True
                ).first.click()
                page.get_by_text("Review this finding", exact=True).click()
                page.get_by_label("Decision", exact=True).select_option("accepted")
                page.get_by_label("Reason", exact=True).fill(
                    "Checked against the synthetic source."
                )
                page.get_by_role(
                    "button", name="Record finding review", exact=True
                ).click()
                page.goto(trip_url)
                expect(page.locator("[data-trip-status]")).to_contain_text("1 of 2")
                expect(page.locator(".case-map-endpoint")).to_have_count(1)
                expect(
                    page.locator(".map-location-list").get_by_text(
                        "Reviewed address estimate", exact=False
                    )
                ).to_be_visible()
                self.capture(page, engine, "trip-reviewed-estimate")
                context.close()
                browser.close()

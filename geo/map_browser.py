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
from core.analysis.address_estimates import set_address_estimate
from core.analysis.services import start_run
from core.analysis.worker import process_run
from geo.analysis.external.geocoding import Geocoder
from geo.models import LocationEvidence, PointOfInterest, VehicleLocationEvidence
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
                speed_kmh=(0, 45, 75, None)[index // 127],
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
        speed_colors = ["18,100,94", "33,83,173", "165,59,27", "86,99,109"]
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
                home = reverse("core:analysis:index", args=(self.case.pk,))
                page.goto(self.live_server_url + home)
                page.get_by_role("link", name="Trips", exact=True).press("Enter")
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
                expect(page.locator("[data-trip-status]")).to_have_class("sr-only")
                expect(page.locator("[data-live-refresh]")).to_have_count(0)
                expect(
                    page.get_by_text("Line color and pattern show reported speed")
                ).to_be_visible()
                # The synthetic observations lie on a straight eastbound line.
                # Read the colored bands in order and count protruding arrow shapes.
                path = page.locator(".trip-path-canvas").evaluate(
                    """(canvas, speedColors) => {
                    const scale = window.devicePixelRatio;
                    const leftToRight = new Set();
                    const colors = new Set(speedColors);
                    const { width, height } = canvas;
                    const radius = Math.ceil(10 * scale);
                    const strip = canvas.getContext("2d").getImageData(
                        0, Math.floor(height / 2) - radius, width, radius * 2 + 1
                    );
                    let arrows = 0;
                    const runs = {};
                    let previousBand;
                    let previousColumnHadArrow = false;
                    for (let x = 0; x < strip.width; x++) {
                        let arrow = false;
                        let band;
                        for (let y = 0; y < strip.height; y++) {
                            const offset = (y * strip.width + x) * 4;
                            if (strip.data[offset + 3] < 240) continue;
                            const rgb = strip.data.subarray(offset, offset + 3)
                                .join(",");
                            if (colors.has(rgb)) {
                                leftToRight.add(rgb);
                                band = rgb;
                            }
                            if (Math.abs(y - radius) > 3 * scale) arrow = true;
                        }
                        if (arrow && !previousColumnHadArrow) arrows++;
                        previousColumnHadArrow = arrow;
                        if (band && band !== previousBand) {
                            runs[band] = (runs[band] || 0) + 1;
                        }
                        previousBand = band;
                    }
                    return { colors: [...leftToRight], arrows, runs };
                }""",
                    speed_colors,
                )
                self.assertEqual(path["colors"], speed_colors)
                self.assertGreaterEqual(path["arrows"], 2)
                self.assertGreater(path["runs"][speed_colors[1]], 2)
                self.assertGreater(path["runs"][speed_colors[2]], 2)
                self.assertGreater(path["runs"][speed_colors[3]], 2)
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
                    expect(page.get_by_label("Vehicle", exact=True)).to_be_visible()
                    expect(
                        page.get_by_label(re.compile(r"^Jump to date"))
                    ).to_be_visible()
                    page.wait_for_function("""() => {
                        const map = document.querySelector("#investigation-map");
                        const drawing = map.querySelector(".trip-path-canvas");
                        return drawing.style.width === `${map.clientWidth}px`
                            && drawing.style.height === `${map.clientHeight}px`;
                    }""")
                    show.click()
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
                page.locator(".trip-list").get_by_role(
                    "link", name=re.compile("Synthetic van")
                ).click()
                expect(
                    page.get_by_role("heading", name="Possible trip", exact=True)
                ).to_be_visible()
                outcome = page.get_by_role("region", name="Synthetic van", exact=True)
                expect(
                    outcome.get_by_role("link", name="0 Example Street")
                ).to_be_visible()
                expect(
                    outcome.get_by_role("link", name="504 Example Street")
                ).to_be_visible()
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
                    self.capture(page, engine, f"trip-outcome-{width}x{height}")
                expect(
                    page.get_by_role("heading", name="Trip observations", exact=True)
                ).to_be_visible()
                expect(page.locator(".map-location-list > li")).to_have_count(50)
                expect(page.locator(".map-location-list > li").first).to_contain_text(
                    "Reported speed 0 km/h"
                )
                page.get_by_role("link", name="Next observations").click()
                page.wait_for_url(
                    re.compile(r"\?sources_page=2#trip-list$"), wait_until="load"
                )
                expect(page.locator(".map-location-list > li").first).to_contain_text(
                    "50 Example Street"
                )
                page.locator(".map-location-list a").first.press("Enter")
                expect(
                    page.get_by_role(
                        "heading", name="Synthetic trip source 50", exact=True
                    )
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name="View possible trip")
                ).to_have_attribute(
                    "href",
                    reverse("geo:trip_detail", args=(self.case.pk, self.trip.pk)),
                )
                page.go_back()
                expect(page.locator(".map-location-list > li").first).to_contain_text(
                    "50 Example Street"
                )
                self.assertEqual(errors, [])
                context.close()
                browser.close()

    def test_stale_geometry_cache_fetches_current_reported_positions(self):
        vehicle = Vehicle.objects.create(case=self.case, make="Small synthetic van")
        for index in range(2):
            VehicleLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                vehicle=vehicle,
                title=f"Reported position {index + 1}",
                latitude="43.64",
                longitude=Decimal("-79.38") + Decimal(index) / 100,
                observed_at=datetime(2026, 11, 1, 12, tzinfo=UTC)
                + timedelta(minutes=index),
            )
        self.build_trips(vehicle_ids=[vehicle.pk])
        trip = saved_trips(self.case).get(vehicle=vehicle)
        url = self.live_server_url + reverse(
            "geo:trip_detail", args=(self.case.pk, trip.pk)
        )
        recheck = re.compile(r"/map/trips/.+/\?recheck=1")
        with sync_playwright() as playwright:
            for engine in self.engines():
                with self.subTest(engine=engine):
                    browser = getattr(playwright, engine).launch()
                    context = browser.new_context(reduced_motion="reduce")
                    self.authenticate(context)
                    self.mock_tiles(context, [])
                    page = context.new_page()
                    # The initial fit schedules its own replay. Let it finish
                    # before rejecting the cache on a subsequent user zoom.
                    page.goto(url)
                    status = page.locator("[data-trip-status]")
                    expect(status).to_contain_text("2 of 2")
                    map_section = page.locator("[data-case-map]")
                    expect(map_section).to_have_attribute("data-trip-complete", "true")
                    while_rechecking = []

                    def reject_cache(
                        route, *, snapshots=while_rechecking, status=status, page=page
                    ):
                        try:
                            has_pixels = page.locator(".trip-path-canvas").evaluate(
                                """canvas => {
                                    const {width, height} = canvas;
                                    if (!width || !height) return false;
                                    const context = canvas.getContext('2d');
                                    const pixels = context.getImageData(
                                        0, 0, width, height
                                    );
                                    return pixels.data.some(
                                        (value, index) => index % 4 === 3 && value
                                    );
                                }"""
                            )
                            snapshots.append(
                                (
                                    status.inner_text(),
                                    status.is_visible(),
                                    page.locator(".case-map-endpoint").count(),
                                    has_pixels,
                                )
                            )
                        finally:
                            route.fulfill(status=409)

                    page.route(recheck, reject_cache)
                    with (
                        page.expect_response(
                            lambda response: (
                                recheck.search(response.url) and response.status == 409
                            )
                        ),
                        page.expect_response(
                            lambda response: (
                                response.url == url + "?cursor=0"
                                and response.status == 200
                            )
                        ),
                    ):
                        page.get_by_role("button", name="Zoom in").click()
                    self.assertTrue(while_rechecking)
                    for pending in while_rechecking:
                        self.assertEqual(
                            pending, ("Loading the complete trip path…", True, 0, False)
                        )
                    expect(status).to_contain_text("2 of 2")
                    expect(map_section).to_have_attribute("data-trip-complete", "true")
                    context.close()
                    browser.close()

    def test_fallback_trip_tools_tiles_and_geometry_failure(self):
        with sync_playwright() as playwright:
            for engine in self.engines():
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(
                    java_script_enabled=False, viewport={"width": 390, "height": 844}
                )
                self.authenticate(context)
                page = context.new_page()
                page.goto(self.map_url)
                vehicle = page.get_by_label("Vehicle", exact=True)
                day = page.get_by_label(re.compile(r"^Jump to date"))
                expect(vehicle).to_be_visible()
                expect(day).to_be_visible()
                expect(page.get_by_label("Search POIs", exact=True)).to_be_hidden()
                vehicle.select_option(str(self.vehicle.pk))
                day.fill("2026-11-01")
                page.get_by_role("button", name="Go", exact=True).press("Enter")
                expect(page).to_have_url(re.compile(r"#trip-day-result$"))
                expect(vehicle).to_have_value(str(self.vehicle.pk))
                expect(vehicle).to_be_visible()
                expect(day).to_have_value("2026-11-01")
                expect(page.locator("#trip-day-result")).to_contain_text(
                    "Synthetic van"
                )
                self.capture(page, engine, "trips-jump-no-js", wait_for_trip=False)
                trips = page.get_by_role("region", name="Possible trips", exact=True)
                self.assertTrue(
                    trips.evaluate("""element => Boolean(
                    element.compareDocumentPosition(document.querySelector('[data-case-map]'))
                    & Node.DOCUMENT_POSITION_FOLLOWING
                )""")
                )
                page.locator(".trip-list").get_by_role(
                    "link", name=re.compile("Synthetic van")
                ).click()
                expect(
                    page.get_by_role(
                        "region", name="Synthetic van", exact=True
                    ).get_by_role("link", name="0 Example Street", exact=True)
                ).to_be_visible()
                self.capture(page, engine, "trip-no-js")
                page.get_by_role("link", name="Back to trips", exact=True).press(
                    "Enter"
                )
                expect(vehicle).to_have_value(str(self.vehicle.pk))
                expect(day).to_have_value("2026-11-01")
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
                    page.locator(".trip-list").get_by_role(
                        "link", name=re.compile("Synthetic van")
                    )
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
                # The WSGI fixture exercises the map with polling.
                context.add_init_script("window.EventSource = undefined;")
                page = context.new_page()
                errors = []
                page.on(
                    "pageerror", lambda error, errors=errors: errors.append(str(error))
                )
                filters = "?q=Alex+Example"
                filtered_map = self.map_url + filters
                filtered_trip = self.detail_url + filters
                page.goto(filtered_trip)
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
                expect(
                    page.get_by_role("link", name="Cancel", exact=True)
                ).to_have_attribute(
                    "href",
                    filtered_trip.removeprefix(self.live_server_url) + "#map-heading",
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
                latitude_field = page.get_by_label("Latitude", exact=True)
                longitude_field = page.get_by_label("Longitude", exact=True)
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_function(
                        "width => window.innerWidth === width", arg=width
                    )
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width
                    )
                    expect(latitude_field).to_be_visible()
                    expect(longitude_field).to_be_visible()
                    # Sample one layout so WebKit scrolling cannot shift just one box.
                    latitude, longitude = latitude_field.evaluate("""latitude => [
                        latitude.getBoundingClientRect().toJSON(),
                        latitude.form.elements.longitude.getBoundingClientRect().toJSON(),
                    ]""")
                    if width >= 768:
                        self.assertEqual(latitude["y"], longitude["y"])
                        self.assertLess(latitude["x"], longitude["x"])
                    else:
                        self.assertEqual(latitude["x"], longitude["x"])
                        self.assertLess(latitude["y"], longitude["y"])
                    self.capture(page, engine, f"poi-form-{width}x{height}")
                page.get_by_role("button", name="Save changes", exact=True).click()
                label = f"Meeting place {engine} · Alex Example"
                expect(
                    page.get_by_role("heading", name=label, exact=True)
                ).to_be_visible()
                self.in_database(
                    lambda: PointOfInterest.objects.get_or_create(
                        person=self.person,
                        kind="work",
                        defaults={
                            "latitude": "43.642",
                            "longitude": "-79.382",
                            "created_by": self.owner,
                        },
                    )
                )
                page.get_by_role("link", name="Back to trip", exact=True).click()
                page.wait_for_url(
                    filtered_trip + "#map-heading", wait_until="domcontentloaded"
                )
                page.goto(filtered_map)
                page.bring_to_front()
                show = page.get_by_role(
                    "button", name=f"Show {label} on map", exact=True
                )
                show.scroll_into_view_if_needed()
                show.press("Enter")
                expect(canvas).to_be_focused()
                expect(page.locator(".leaflet-popup-content")).to_contain_text("POI")
                expect(page.locator(".leaflet-popup-content")).to_contain_text(label)
                expect(
                    page.locator(".leaflet-popup-content").get_by_role(
                        "link", name=re.compile("Work · Alex Example")
                    )
                ).to_be_visible()
                expect(
                    page.locator(".case-map-poi").filter(has_text="POIs")
                ).to_have_count(1)
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
                page.goto(filtered_map)
                show.scroll_into_view_if_needed()
                show.press("Enter")
                page.locator(".leaflet-popup-content").get_by_role(
                    "link", name=re.compile(re.escape(label))
                ).click()
                page.get_by_role("link", name="Back to map", exact=True).click()
                expect(page).to_have_url(filtered_map + "#map-heading")
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
                filtered_person = person_url + "?q=Alex&page=2"
                page.goto(filtered_person)
                page.get_by_role("link", name=label, exact=True).click()
                page.get_by_role("link", name="Back to person", exact=True).click()
                expect(page).to_have_url(filtered_person + "#poi-list")
                page.get_by_role("link", name="Add POI", exact=True).click()
                page.get_by_role("link", name="Cancel", exact=True).press("Enter")
                expect(page).to_have_url(filtered_person + "#poi-list")
                page.get_by_role("link", name="Add POI", exact=True).press("Enter")
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
                work_detail = page.url
                self.capture(page, engine, "poi-detail-no-js")
                page.locator("summary").filter(has_text="Record details").focus()
                page.locator("summary").filter(has_text="Record details").press("Enter")
                page.get_by_role("link", name="Delete POI", exact=True).click()
                page.get_by_role("link", name="Cancel", exact=True).press("Enter")
                expect(page).to_have_url(work_detail)
                page.locator("summary").filter(has_text="Record details").press("Enter")
                page.get_by_role("link", name="Delete POI", exact=True).press("Enter")
                page.get_by_label(
                    "I understand this permanently deletes this POI.", exact=True
                ).check()
                page.get_by_role(
                    "button", name="Permanently delete POI", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Alex Example", exact=True)
                ).to_be_visible()
                expect(page).to_have_url(filtered_person + "#poi-list")
                expect(page.locator(".message[role=status]")).to_contain_text(
                    "POI deleted"
                )
                context.close()
                browser.close()

    @override_settings(**LOCAL_GEOCODER)
    def test_address_trip_maps_selected_estimate(self):
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
                    lambda: set_address_estimate(
                        self.owner, self.case.pk, run.pk, 0, use=False
                    )
                )
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(viewport={"width": 390, "height": 844})
                self.authenticate(context)
                self.mock_tiles(context, [])
                page = context.new_page()
                page.goto(trip_url)
                expect(page.locator("[data-trip-status]")).to_contain_text(
                    "Choose address estimates"
                )
                expect(page.locator(".case-map-endpoint")).to_have_count(0)
                page.get_by_role("link", name="Findings (2)", exact=True).press("Enter")
                findings = page.get_by_role(
                    "region", name="Findings from trip observations"
                )
                expect(findings.locator(".analysis-finding-rows > li")).to_have_count(2)
                findings.get_by_role("link", name=records[1].title, exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role("heading", name=records[1].title, exact=True)
                ).to_be_visible()
                page.go_back()
                expect(findings).to_be_in_viewport()
                page.get_by_role(
                    "link", name="Review address estimates for this trip", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Address estimates", exact=True)
                ).to_be_visible()
                page.get_by_role(
                    "link", name="Open address lookup", exact=True
                ).first.click()
                page.get_by_role("button", name="Use this estimate", exact=True).click()
                expect(
                    page.get_by_role(
                        "button", name="Stop using this estimate", exact=True
                    )
                ).to_be_visible()
                page.goto(trip_url)
                expect(page.locator("[data-trip-status]")).to_contain_text("1 of 2")
                expect(page.locator("[data-trip-status]")).not_to_have_class("sr-only")
                expect(page.locator(".case-map-endpoint")).to_have_count(1)
                expect(
                    page.locator(".map-location-list").get_by_text(
                        "Selected address estimate", exact=False
                    )
                ).to_be_visible()
                self.capture(page, engine, "trip-selected-estimate")
                self.in_database(
                    lambda: set_address_estimate(
                        self.owner, self.case.pk, run.pk, 0, use=False
                    )
                )
                with page.expect_response(
                    lambda response: (
                        response.url.startswith(f"{trip_url}?cursor=0")
                        and response.status == 200
                    )
                ):
                    page.get_by_role("button", name="Zoom out", exact=True).press(
                        "Enter"
                    )
                expect(page.locator("[data-trip-status]")).to_contain_text(
                    "Choose address estimates"
                )
                expect(page.locator(".case-map-endpoint")).to_have_count(0)
                context.close()
                browser.close()

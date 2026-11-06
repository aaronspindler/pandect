"""Explicit synthetic map journey: python manage.py test cases.map_browser."""

import io
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connections
from django.test import override_settings
from django.urls import reverse
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

from analysis.geocoding import Geocoder
from analysis.models import AnalysisRun
from analysis.services import process_run
from cases.models import Case
from cases.tests.test_map_geocoding import LOCAL_GEOCODER
from evidence.models import LocationEvidence
from people.models import Person


@override_settings(
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    PANDECT_MAP_TILE_URL="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    PANDECT_MAP_TILE_ORIGIN="https://tile.openstreetmap.org",
)
class InvestigationMapBrowserTests(StaticLiveServerTestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user(
            username="map-investigator", email="map@example.test"
        )
        EmailAddress.objects.create(
            user=owner, email=owner.email, verified=True, primary=True
        )
        self.case = Case.objects.create(
            owner=owner, name="Synthetic harbour investigation"
        )
        self.person = Person.objects.create(case=self.case, first_name="Alex Example")
        self.locations = []
        for title, latitude, longitude in (
            ("Harbour entrance", "43.640", "-79.380"),
            ("Harbour second source", "43.640", "-79.380"),
            ("Reported street corner", "43.645", "-79.389"),
            ('<img src=x onerror="alert(1)">', "43.643", "-79.383"),
        ):
            self.locations.append(
                LocationEvidence.objects.create(
                    case=self.case,
                    uploaded_by=owner,
                    title=title,
                    latitude=latitude,
                    longitude=longitude,
                    source="Synthetic fixture",
                )
            )
        LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=owner,
            title="Address without coordinates",
            address="Synthetic address only",
        )
        self.client.force_login(owner)
        self.cookie = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.map_url = self.live_server_url + reverse(
            "cases:case_map", args=(self.case.pk,)
        )
        self.artifacts = Path("data/validation/maps").resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        image = Image.new("RGB", (256, 256), "#edf0e4")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((0, 150, 256, 256), fill="#d4e5ec")
        for offset in (40, 100, 180):
            drawing.line((offset, 0, offset + 60, 256), fill="#ffffff", width=9)
            drawing.line((0, offset, 256, offset - 20), fill="#ffffff", width=7)
        drawing.text((10, 10), "Synthetic tile", fill="#515154")
        image.save(buffer, "PNG")
        self.tile = buffer.getvalue()

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

    def capture(self, page, engine, name):
        page.evaluate("window.scrollTo(0, 0)")
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
                show = page.get_by_role(
                    "button", name=f"Show {label} on map", exact=True
                )
                show.focus()
                page.keyboard.press("Enter")
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
    def test_local_address_estimates_update_without_external_evidence_requests(self):
        candidates = [
            {
                "latitude": 43.641,
                "longitude": -79.381,
                "granularity": "street",
                "licence": "Synthetic local licence",
            },
            {
                "latitude": 43.646,
                "longitude": -79.387,
                "granularity": "neighbourhood",
                "licence": "Synthetic local licence",
            },
        ]
        with (
            patch.object(Geocoder, "lookup", return_value=candidates),
            sync_playwright() as playwright,
        ):
            for engine in self.engines():
                title = f"Address to locate in {engine}"
                self.in_database(
                    lambda title=title, engine=engine: LocationEvidence.objects.create(
                        case=self.case,
                        uploaded_by=self.case.owner,
                        title=title,
                        address=f"1 Synthetic {engine} Road",
                    )
                )
                browser = getattr(playwright, engine).launch()
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                self.authenticate(context)
                external = []

                def serve(route, _request, *, external=external):
                    if route.request.url.startswith(self.live_server_url + "/"):
                        route.continue_()
                    else:
                        external.append(route.request)
                        route.fulfill(
                            status=200, content_type="image/png", body=self.tile
                        )

                context.route("**/*", serve)
                page = context.new_page()
                page.set_default_timeout(15000)
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and "/map/locate/" in response.url
                    )
                ) as queued:
                    page.goto(self.map_url)
                self.assertEqual(queued.value.status, 200)
                page.locator(".map-search > summary").click()
                page.get_by_label("Search locations").focus()

                def work():
                    for run in AnalysisRun.objects.filter(
                        case=self.case, status="queued"
                    ):
                        process_run(str(run.pk))

                self.in_database(work)
                expect(page.locator(".case-map-estimate")).to_have_count(2)
                expect(page.get_by_label("Search locations")).to_be_focused()
                expect(page.locator("[data-geocode-status]")).to_contain_text(
                    "finished"
                )
                expect(page.locator("[data-map-estimate-key]")).to_be_visible()
                page.get_by_role("button", name=f"Show {title} on map").focus()
                page.keyboard.press("Enter")
                expect(page.locator(".leaflet-popup-content")).to_contain_text(
                    "Unreviewed address estimate"
                )
                # Exercise later status replies while a source or review link
                # owns keyboard focus, without starting more worker jobs.
                snapshot = page.request.get(
                    self.map_url, headers={"Accept": "application/json"}
                ).json()
                update = dict(snapshot)
                form = page.locator("[data-map-geocode]")
                action = form.get_attribute("action")
                queue_url = self.live_server_url + action
                page.route(queue_url, lambda route: route.fulfill(json={}))
                page.route(
                    self.map_url,
                    lambda route, request, update=update: route.fulfill(json=update),
                )
                popup = page.locator(".leaflet-popup-content")
                source_number = None
                for index in (0, 1):
                    link = popup.get_by_role("link").nth(index)
                    link.focus()
                    href = link.get_attribute("href")
                    source_number = int(link.get_attribute("data-map-number"))
                    update["points"] = [
                        point | {"label": f"Refreshed estimate {index}"}
                        if point.get("derived")
                        else point
                        for point in snapshot["points"]
                    ]
                    form.evaluate("form => form.requestSubmit()")
                    expect(popup).to_contain_text(f"Refreshed estimate {index}")
                    expect(popup.get_by_role("link").nth(index)).to_be_focused()
                    self.assertEqual(
                        popup.get_by_role("link").nth(index).get_attribute("href"),
                        href,
                    )
                update["points"] = [
                    point
                    for point in snapshot["points"]
                    if point["number"] != source_number
                ]
                form.evaluate("form => form.requestSubmit()")
                expect(popup).to_have_count(0)
                expect(page.locator("#investigation-map")).to_be_focused()
                update["points"] = snapshot["points"]
                form.evaluate("form => form.requestSubmit()")
                show = page.get_by_role("button", name=f"Show {title} on map")
                expect(show).to_be_visible()
                page.unroute(queue_url)
                page.unroute(self.map_url)
                # Reserve space as a scrollbar or containing layout can.
                page.locator("#investigation-map").evaluate(
                    "node => node.style.width = 'calc(100% - 32px)'"
                )
                show.click()
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    page.get_by_role("button", name="Show all mapped locations").click()
                    page.wait_for_function("""() => {
                      const popupNode = document.querySelector('.leaflet-popup');
                      const mapNode = document.getElementById('investigation-map');
                      const popup = popupNode.getBoundingClientRect();
                      const map = mapNode.getBoundingClientRect();
                      return popup.left >= map.left && popup.right <= map.right &&
                             popup.top >= map.top && popup.bottom <= map.bottom;
                    }""")
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width
                    )
                    self.capture(page, engine, f"map-local-estimates-{width}x{height}")
                row = page.locator(".map-location-list > li").filter(
                    has=page.get_by_role("link", name=title, exact=True)
                )
                expect(row).to_contain_text("2 address estimates")
                row.get_by_role("link", name="Review lookup", exact=True).click()
                expect(
                    page.get_by_role(
                        "heading",
                        name="Look up geographic coordinates or addresses",
                        exact=True,
                    )
                ).to_be_visible()
                self.assertTrue(external)
                self.assertTrue(
                    all(
                        re.fullmatch(
                            r"https://tile\.openstreetmap\.org/\d+/\d+/\d+\.png",
                            request.url,
                        )
                        and request.method == "GET"
                        and request.post_data is None
                        for request in external
                    )
                )
                context.close()
                browser.close()

    def test_map_loading_keyboard_sources_and_responsive_layouts(self):
        with sync_playwright() as playwright:
            for engine in self.engines():
                with self.subTest(engine=engine):
                    browser = getattr(playwright, engine).launch()
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
                    )
                    self.authenticate(context)
                    requests = []

                    def serve_tile(route, _request, *, requests=requests):
                        if route.request.url.startswith(self.live_server_url + "/"):
                            route.continue_()
                            return
                        requests.append(route.request)
                        route.fulfill(
                            status=200, content_type="image/png", body=self.tile
                        )

                    # Only numeric tile paths may leave the page, never source
                    # evidence or identifiers. Mock every external request.
                    context.route("**/*", serve_tile)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    errors = []
                    page.on(
                        "pageerror",
                        lambda error, errors=errors: errors.append(str(error)),
                    )
                    page.goto(self.map_url)
                    expect(
                        page.get_by_role("heading", name="Map", exact=True)
                    ).to_be_visible()
                    expect(
                        page.get_by_role("link", name="Map", exact=True)
                    ).to_have_attribute("aria-current", "page")
                    expect(page.locator("[data-map-status]")).to_contain_text(
                        "Map loaded"
                    )
                    expect(page.locator("[data-map-load]")).to_have_count(0)
                    expect(page.locator("#investigation-map")).not_to_be_focused()
                    page.locator("[data-case-map] .actions a").first.focus()
                    page.keyboard.press("Enter")
                    expect(
                        page.get_by_role("heading", name="Locations", exact=True)
                    ).to_be_in_viewport()
                    page.get_by_text("Map details", exact=True).focus()
                    page.keyboard.press("Enter")
                    expect(
                        page.get_by_text(
                            "Evidence files, titles, notes, and source details "
                            "stay in Pandect.",
                            exact=False,
                        )
                    ).to_be_visible()
                    page.keyboard.press("Enter")
                    self.assertTrue(requests)
                    self.assertTrue(
                        all(
                            request.headers.get("referer") == self.live_server_url + "/"
                            for request in requests
                        )
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
                    expect(page.locator(".case-map-marker")).to_have_count(3)
                    marker = page.get_by_role(
                        "button", name="2 observations at this location"
                    )
                    marker.focus()
                    page.keyboard.press("Enter")
                    expect(
                        page.locator(".leaflet-popup-content").get_by_role("link")
                    ).to_have_count(2)
                    expect(page.locator(".leaflet-popup-content")).to_contain_text(
                        "Harbour entrance"
                    )
                    page.keyboard.press("Escape")
                    show = page.get_by_role(
                        "button", name="Show Reported street corner on map"
                    )
                    show.focus()
                    page.keyboard.press("Enter")
                    expect(page.locator("#investigation-map")).to_be_focused()
                    expect(page.locator(".leaflet-popup-content")).to_contain_text(
                        "Reported street corner"
                    )
                    page.keyboard.press("Escape")
                    page.keyboard.press("ArrowRight")
                    page.keyboard.press("+")
                    page.get_by_role("button", name="Show all mapped locations").click()
                    expect(
                        page.locator(".case-map .leaflet-control-attribution")
                    ).to_contain_text("OpenStreetMap contributors")
                    self.assertEqual(page.locator(".map-location-list img").count(), 0)
                    for width, height in (
                        (1440, 900),
                        (1440, 1024),
                        (768, 1024),
                        (390, 844),
                        (320, 844),
                    ):
                        page.set_viewport_size({"width": width, "height": height})
                        page.get_by_role(
                            "button", name="Show all mapped locations"
                        ).click()
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"), width
                        )
                        self.capture(page, engine, f"map-{width}x{height}")
                    page.emulate_media(color_scheme="dark")
                    self.capture(page, engine, "map-dark")
                    if engine == "chromium":
                        page.emulate_media(forced_colors="active")
                        self.capture(page, engine, "map-forced-colors")
                        page.emulate_media(forced_colors="none")
                    context.route(
                        "https://tile.openstreetmap.org/**", lambda route: route.abort()
                    )
                    page.reload()
                    expect(page.locator("[data-map-status]")).to_contain_text(
                        "Some map tiles could not load"
                    )
                    expect(
                        page.get_by_role(
                            "link", name="Address without coordinates", exact=True
                        )
                    ).to_be_visible()
                    self.assertEqual(errors, [])
                    context.close()
                    browser.close()

    def test_without_javascript_and_missing_library(self):
        with sync_playwright() as playwright:
            for engine in self.engines():
                browser = getattr(playwright, engine).launch()
                for javascript in (False, True):
                    with self.subTest(engine=engine, javascript=javascript):
                        context = browser.new_context(
                            java_script_enabled=javascript,
                            viewport={"width": 390, "height": 844},
                        )
                        self.authenticate(context)
                        if javascript:
                            context.route(
                                "**/vendor/leaflet/leaflet.js",
                                lambda route: route.abort(),
                            )
                        page = context.new_page()
                        page.goto(self.map_url)
                        expect(
                            page.get_by_role(
                                "link", name="Harbour entrance", exact=True
                            )
                        ).to_be_visible()
                        expect(page.locator("[data-map-load]")).to_have_count(0)
                        if javascript:
                            expect(page.locator("[data-map-status]")).to_contain_text(
                                "could not start"
                            )
                        else:
                            expect(
                                page.get_by_text(
                                    "Enable JavaScript to load the interactive map.",
                                    exact=False,
                                )
                            ).to_be_visible()
                        self.capture(page, engine, f"map-fallback-js-{javascript}")
                        expect(page.get_by_label("Search locations")).to_be_hidden()
                        search_toggle = page.locator(".map-search > summary")
                        search_toggle.focus()
                        search_toggle.press("Enter")
                        page.get_by_label("Search locations").fill("Harbour")
                        page.get_by_label("Evidence type", exact=True).select_option(
                            "location"
                        )
                        page.get_by_label("Status", exact=True).select_option("all")
                        page.get_by_role("button", name="Apply filters").click()
                        expect(page.locator(".map-search")).to_have_attribute(
                            "open", ""
                        )
                        expect(search_toggle).to_contain_text("Applied")
                        expect(page.get_by_label("Search locations")).to_have_value(
                            "Harbour"
                        )
                        expect(
                            page.get_by_label("Evidence type", exact=True)
                        ).to_have_value("location")
                        expect(page.get_by_label("Status", exact=True)).to_have_value(
                            "all"
                        )
                        expect(page.locator(".map-location-list > li")).to_have_count(2)
                        self.capture(page, engine, f"map-filters-js-{javascript}")
                        page.get_by_role("link", name="Clear filters").click()
                        expect(page.get_by_label("Search locations")).to_be_hidden()
                        page.get_by_role(
                            "link", name="Harbour entrance", exact=True
                        ).click()
                        expect(
                            page.get_by_role(
                                "heading", name="Harbour entrance", exact=True
                            )
                        ).to_be_visible()
                        context.close()
                browser.close()

    @override_settings(PANDECT_MAP_TILE_URL="", PANDECT_MAP_TILE_ORIGIN="")
    def test_source_markers_work_without_any_tile_requests(self):
        with sync_playwright() as playwright:
            for engine in self.engines():
                with self.subTest(engine=engine):
                    browser = getattr(playwright, engine).launch()
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 900}
                    )
                    self.authenticate(context)
                    requests = []

                    def block_external(route, *, requests=requests):
                        requests.append(route.request.url)
                        route.abort()

                    context.route("https://**", block_external)
                    page = context.new_page()
                    page.goto(self.map_url)
                    expect(page.locator("[data-map-status]")).to_contain_text(
                        "No background map configured"
                    )
                    expect(page.locator(".case-map-marker")).to_have_count(3)
                    show = page.get_by_role(
                        "button", name="Show Reported street corner on map"
                    )
                    show.focus()
                    page.keyboard.press("Enter")
                    expect(page.locator("#investigation-map")).to_be_focused()
                    expect(page.locator(".leaflet-popup-content")).to_contain_text(
                        "Reported street corner"
                    )
                    for width, height in ((1440, 900), (390, 844), (320, 844)):
                        page.set_viewport_size({"width": width, "height": height})
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"), width
                        )
                        self.capture(page, engine, f"map-no-tiles-{width}x{height}")
                    self.assertEqual(requests, [])
                    context.close()
                    browser.close()

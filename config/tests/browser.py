"""Explicit browser journeys: python manage.py test config.tests.browser.

This module is intentionally outside default ``test*.py`` discovery. Install the
Playwright browsers first; use PANDECT_BROWSER_ENGINES to select engines and
PANDECT_BROWSER_ARTIFACTS to choose the synthetic screenshot directory.
"""

import hashlib
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from email import policy
from email.parser import BytesParser
from pathlib import Path

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

from cases.models import Case, CaseInvitation, CaseMembership
from evidence.models import (
    AudioEvidence,
    DocumentEvidence,
    ImageEvidence,
    LocationEvidence,
    PersonLocationEvidence,
    TextEvidence,
    VehicleLocationEvidence,
)
from evidence.tests.test_vehicle_imports import csv_content
from people.models import Person
from vehicles.models import Vehicle


@override_settings(
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    ACCOUNT_RATE_LIMITS=False,
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
)
class CoreInterfaceBrowserTests(StaticLiveServerTestCase):
    password = "synthetic-browser-correct-horse-742!"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engines = tuple(
            engine.strip()
            for engine in os.environ.get(
                "PANDECT_BROWSER_ENGINES", "chromium,webkit"
            ).split(",")
            if engine.strip()
        )
        if not cls.engines or set(cls.engines) - {"chromium", "webkit"}:
            raise ValueError(
                "PANDECT_BROWSER_ENGINES must contain chromium and/or webkit."
            )

    def setUp(self):
        super().setUp()
        # Playwright's sync API runs an event loop on the calling thread. Start
        # it only after fixtures, stop it before Django teardown, and execute
        # in-journey ORM assertions in a synchronous worker. Django's async
        # safety checks remain enabled in both the tests and application.
        self.playwright = None
        self.browsers = {}
        self.browser_context_number = 0
        self.database_worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.database_worker.shutdown)
        self.media_directory = tempfile.TemporaryDirectory(prefix="pandect-browser-")
        self.addCleanup(self.media_directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=self.media_directory.name))
        self.artifacts = Path(
            os.environ.get("PANDECT_BROWSER_ARTIFACTS", "data/validation/browser")
        ).resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.owner = self.make_user("browser-owner")
        self.case = Case.objects.create(
            owner=self.owner,
            name="Harbour records",
            description="Synthetic browser investigation; no real evidence.",
        )
        self.records = [
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=(f"Source {number:02d}" if number < 25 else "Interview notes"),
                body=(
                    "Synthetic source excerpt for interface validation.\n\n"
                    "The harbour office was closed at 16:30. This statement "
                    "is a synthetic observation, not a verified conclusion."
                ),
                source_description="Synthetic interview",
                description="Investigator context is separate from the original.",
            )
            for number in range(1, 26)
        ]

    def make_user(self, username):
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password=self.password,
        )
        EmailAddress.objects.create(
            user=user, email=user.email, primary=True, verified=True
        )
        return user

    def url(self, name, **kwargs):
        return self.live_server_url + reverse(name, kwargs=kwargs)

    def db(self, operation, *args, **kwargs):
        def run():
            try:
                return operation(*args, **kwargs)
            finally:
                connections.close_all()

        return self.database_worker.submit(run).result()

    @contextmanager
    def browser_page(self, engine, **options):
        outermost = self.playwright is None
        if outermost:
            self.playwright = sync_playwright().start()
        context = None
        page = None
        self.browser_context_number += 1
        context_number = self.browser_context_number
        try:
            if engine not in self.browsers:
                self.browsers[engine] = getattr(self.playwright, engine).launch()
            context = self.browsers[engine].new_context(
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="UTC",
                color_scheme="light",
                accept_downloads=True,
                **options,
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            script_errors = []
            page.on("pageerror", lambda error: script_errors.append(str(error)))
            yield context, page
            self.assertEqual(script_errors, [], "Unexpected browser script errors")
        except Exception:
            # Preserve the failing state without masking the original assertion.
            if page is not None and not page.is_closed():
                with suppress(PlaywrightError, OSError):
                    page.screenshot(
                        path=str(
                            self.artifacts
                            / (
                                f"{engine}-failure-{self._testMethodName}"
                                f"-context-{context_number}.png"
                            )
                        ),
                        timeout=5000,
                    )
            raise
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                if outermost:
                    try:
                        for browser in self.browsers.values():
                            browser.close()
                    finally:
                        self.playwright.stop()
                        self.playwright = None
                        self.browsers = {}

    def sign_in(self, page, user=None):
        user = user or self.owner
        page.goto(self.url("account_login"))
        page.locator("input[name='login']").fill(user.email)
        page.locator("input[name='password']").fill(self.password)
        page.get_by_role("button", name=re.compile(r"^sign in$", re.I)).click()
        expect(
            page.get_by_role("heading", name="All cases", exact=True)
        ).to_be_visible()

    def capture(self, page, engine, name):
        page.screenshot(path=str(self.artifacts / f"{engine}-{name}.png"))

    def test_communication_evidence_and_retained_attachments(self):
        sender = Person.objects.create(case=self.case, first_name="Alex Sender")
        recipient = Person.objects.create(case=self.case, first_name="Jordan Recipient")
        viewer = self.make_user("communications-viewer")
        CaseMembership.objects.create(case=self.case, user=viewer)
        for engine in self.engines:
            for kind in ("text-message", "phone-call", "facetime"):
                with (
                    self.subTest(engine=engine, kind=kind),
                    self.browser_page(engine, java_script_enabled=False) as (_, page),
                ):
                    self.sign_in(page)
                    page.goto(
                        self.url("evidence:create", case_id=self.case.pk)
                        + f"?kind={kind}"
                    )
                    title = f"Synthetic {kind} {engine}"
                    page.get_by_label("Title", exact=True).fill(title)
                    page.get_by_role("button", name=re.compile(r"^Save ")).click()
                    expect(page.get_by_label("Sender", exact=True)).to_be_focused()
                    expect(page.get_by_label("Title", exact=True)).to_have_value(title)
                    page.get_by_label("Sender", exact=True).select_option(
                        str(sender.pk)
                    )
                    page.get_by_label("Recipient", exact=True).select_option(
                        str(recipient.pk)
                    )
                    page.get_by_label("Sender", exact=True).focus()
                    page.get_by_label("Sender", exact=True).press(
                        "Alt+Tab"
                        if engine == "webkit" and sys.platform == "darwin"
                        else "Tab"
                    )
                    expect(page.get_by_label("Recipient", exact=True)).to_be_focused()
                    self.assertNotEqual(
                        page.get_by_label("Recipient", exact=True).evaluate(
                            "node => getComputedStyle(node).outlineStyle"
                        ),
                        "none",
                    )
                    for name in ("Sender", "Recipient"):
                        self.assertGreaterEqual(
                            page.get_by_label(name, exact=True).bounding_box()[
                                "height"
                            ],
                            44,
                        )
                    if kind == "text-message":
                        page.get_by_label("Original message", exact=True).fill(
                            "  Synthetic message <source>\n  "
                        )
                        page.get_by_label("Sent at (UTC)", exact=True).fill(
                            "2026-10-26T12:30:15"
                        )
                    else:
                        page.get_by_label("Call started at (UTC)", exact=True).fill(
                            "2026-10-26T12:30:15"
                        )
                        page.get_by_label("Duration", exact=True).fill("00:00:00")
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
                        if kind != "facetime":
                            page.screenshot(
                                path=str(
                                    self.artifacts / f"{engine}-{kind}-form-{width}.png"
                                ),
                                full_page=True,
                            )
                    page.get_by_role("button", name=re.compile(r"^Save ")).click()
                    expect(
                        page.get_by_role("heading", name=title, exact=True)
                    ).to_be_visible()
                    detail_url = page.url
                    if kind != "text-message":
                        expect(
                            page.get_by_text(
                                "No transcript or audio has been added to this call.",
                                exact=False,
                            )
                        ).to_be_visible()
                        page.get_by_role(
                            "link", name="Add transcript or audio", exact=True
                        ).click()
                        page.get_by_role(
                            "button", name="Add transcript or audio", exact=True
                        ).click()
                        expect(page.locator(".error-summary")).to_be_visible()
                        page.get_by_label("Transcript", exact=True).fill(
                            "  Supplied synthetic transcript <source>\n  "
                        )
                        page.get_by_label("Audio file", exact=True).set_input_files(
                            {
                                "name": "synthetic-call.wav",
                                "mimeType": "audio/wav",
                                "buffer": b"Synthetic audio\x00\r\n",
                            }
                        )
                        self.capture(page, engine, f"{kind}-attachment-mobile")
                        page.get_by_role(
                            "button", name="Add transcript or audio", exact=True
                        ).click()
                        expect(
                            page.get_by_role(
                                "heading", name="synthetic-call.wav", exact=True
                            )
                        ).to_be_visible()
                        with page.expect_download() as event:
                            page.get_by_role(
                                "link", name="Download audio", exact=True
                            ).click()
                        self.assertEqual(
                            Path(event.value.path()).read_bytes(),
                            b"Synthetic audio\x00\r\n",
                        )
                        with page.expect_download() as transcript:
                            page.get_by_role(
                                "link", name="Download transcript", exact=True
                            ).click()
                        self.assertEqual(
                            Path(transcript.value.path()).read_bytes(),
                            # Native forms submit textarea line endings as CRLF.
                            b"  Supplied synthetic transcript <source>\r\n  ",
                        )
                        page.get_by_text(
                            "Attachment integrity details", exact=True
                        ).click()
                        with page.expect_download() as manifest:
                            page.get_by_role(
                                "link", name="Download attachment record", exact=True
                            ).click()
                        self.assertEqual(
                            json.loads(Path(manifest.value.path()).read_bytes())[
                                "schema"
                            ],
                            "pandect.call-attachment.v1",
                        )
                        page.get_by_role(
                            "link", name="Add transcript or audio", exact=True
                        ).click()
                        page.get_by_label("Transcript", exact=True).fill(
                            "Later retained correction"
                        )
                        page.get_by_role(
                            "button", name="Add transcript or audio", exact=True
                        ).click()
                        expect(
                            page.get_by_text("Later retained correction", exact=True)
                        ).to_be_visible()
                        expect(
                            page.get_by_role(
                                "heading", name="synthetic-call.wav", exact=True
                            )
                        ).to_be_visible()
                    with page.expect_download() as event:
                        page.get_by_role(
                            "link", name="Download communication record", exact=True
                        ).click()
                    data = json.loads(Path(event.value.path()).read_bytes())
                    self.assertEqual(data["sender_id"], str(sender.pk))
                    self.assertEqual(data["recipient_id"], str(recipient.pk))
                    for width, height in ((1440, 900), (390, 844), (320, 844)):
                        page.set_viewport_size({"width": width, "height": height})
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"),
                            width + 1,
                        )
                        page.screenshot(
                            path=str(
                                self.artifacts / f"{engine}-{kind}-original-{width}.png"
                            ),
                            full_page=True,
                        )
                    page.emulate_media(
                        color_scheme="dark",
                        forced_colors="active",
                        reduced_motion="reduce",
                    )
                    self.capture(page, engine, f"{kind}-forced-colors")
                    with self.browser_page(engine, java_script_enabled=False) as (
                        _,
                        page,
                    ):
                        self.sign_in(page, viewer)
                        page.goto(detail_url)
                        expect(
                            page.get_by_role("heading", name=title, exact=True)
                        ).to_be_visible()
                        expect(
                            page.get_by_role(
                                "link", name="Add transcript or audio", exact=True
                            )
                        ).to_have_count(0)
                        expect(
                            page.get_by_role("link", name="Edit details", exact=True)
                        ).to_have_count(0)

    def test_vehicle_location_csv_import_journey(self):
        vehicle = Vehicle.objects.create(case=self.case, model="Synthetic van")
        viewer = self.make_user("vehicle-import-viewer")
        CaseMembership.objects.create(case=self.case, user=viewer)
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("vehicles:list", case_id=self.case.pk))
                page.get_by_role("link", name="Import vehicle locations").click()
                expect(
                    page.get_by_role("heading", name="Import vehicle locations")
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
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"vehicle-import-upload-{width}")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.get_by_label("Asset Location CSV").set_input_files(
                    {
                        "name": "synthetic-assets.csv",
                        "mimeType": "text/csv",
                        "buffer": csv_content(),
                    }
                )
                page.get_by_label("Export timezone", exact=True).select_option(
                    "America/Toronto"
                )
                page.get_by_label("Source name or citation").fill(
                    "Synthetic tracking export"
                )
                page.get_by_label(
                    "Use Created Time as the reported observation time"
                ).check()
                page.get_by_role("button", name="Retain CSV and review").click()
                expect(
                    page.get_by_role("heading", name="Review vehicle locations")
                ).to_be_visible()
                page.locator(".import-observation summary").focus()
                page.locator(".import-observation summary").press("Enter")
                expect(
                    page.get_by_text("16 Sep 2026, 18:57:22 UTC", exact=True)
                ).to_be_visible()
                self.assertEqual(page.locator(".import-observation[open]").count(), 1)
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
                    self.capture(page, engine, f"vehicle-import-review-{width}")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "vehicle-import-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "vehicle-import-forced-colors")
                page.emulate_media(forced_colors="none")
                page.locator("select[name=vehicle_0]").select_option(str(vehicle.pk))
                if page.locator("input[name=confirm_duplicates]").count():
                    page.get_by_label(
                        "Keep the possible duplicates as separate observations"
                    ).check()
                confirmation = page.get_by_label(
                    "Import these observations with the selected vehicle associations"
                )
                confirmation.focus()
                confirmation.press("Space")
                expect(confirmation).to_be_checked()
                page.get_by_role(
                    "button", name="Import 1 observation", exact=True
                ).focus()
                page.get_by_role(
                    "button", name="Import 1 observation", exact=True
                ).press("Enter")
                expect(
                    page.get_by_role("heading", name="Vehicle locations imported")
                ).to_be_visible()
                self.capture(page, engine, "vehicle-import-complete")
                page.locator(".import-results a").click()
                expect(
                    page.get_by_role("heading", name="Reported location")
                ).to_be_visible()
                expect(page.get_by_text("0.000 km/h", exact=True)).to_be_visible()
                for width, height in ((1440, 900), (390, 844), (320, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"vehicle-location-original-{width}")
                with page.expect_download() as event:
                    page.get_by_role(
                        "link", name="Download original", exact=True
                    ).click()
                original = json.loads(Path(event.value.path()).read_bytes())
                self.assertEqual(original["schema"], "pandect.vehicle-location.v1")
                self.assertEqual(
                    original["raw_row"]["Serial Number"], "000000000000123"
                )
                detail_url = page.url
                with self.browser_page(engine, java_script_enabled=False) as (
                    _,
                    viewer_page,
                ):
                    self.sign_in(viewer_page, viewer)
                    viewer_page.goto(detail_url)
                    expect(
                        viewer_page.get_by_text("You have Viewer access.", exact=False)
                    ).to_be_visible()
                    expect(
                        viewer_page.get_by_role("link", name="Edit details")
                    ).to_have_count(0)
                    expect(
                        viewer_page.get_by_role(
                            "link", name="Download original", exact=True
                        )
                    ).to_be_visible()
        self.assertEqual(
            self.db(VehicleLocationEvidence.objects.count), len(self.engines)
        )

    def evidence_url(self, record=None, *, case=None):
        return self.url(
            "evidence:detail",
            case_id=(case or self.case).pk,
            kind="text",
            evidence_id=(record or self.records[-1]).pk,
        )

    def test_vehicle_workflow_responsive_keyboard_and_read_only_access(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.url("cases:case_detail", case_id=self.case.pk))
                page.get_by_role("link", name="Vehicles", exact=True).click()
                expect(page.locator('[aria-current="page"]')).to_have_text("Vehicles")
                if engine == self.engines[0]:
                    expect(
                        page.get_by_role(
                            "heading", name="Keep the vehicles in this case together"
                        )
                    ).to_be_visible()
                page.get_by_role("link", name="Add vehicle").first.focus()
                page.get_by_role("link", name="Add vehicle").first.press("Enter")
                page.get_by_label("Investigator notes", exact=True).fill(
                    "Synthetic observation"
                )
                page.get_by_role("button", name="Add vehicle", exact=True).click()
                expect(page.locator(".error-summary")).to_be_focused()
                expect(
                    page.get_by_label("Investigator notes", exact=True)
                ).to_have_value("Synthetic observation")
                make = page.get_by_label("Make", exact=True)
                make.fill("Synthetic Motors")
                make.focus()
                make.press("Tab")
                expect(page.get_by_label("Model", exact=True)).to_be_focused()
                self.assertNotEqual(
                    page.get_by_label("Model", exact=True).evaluate(
                        "node => getComputedStyle(node).outlineStyle"
                    ),
                    "none",
                )
                page.get_by_label("Model", exact=True).fill(f"Hatchback {engine}")
                page.get_by_label("Year", exact=True).fill("2020")
                page.get_by_label("Color", exact=True).fill("Blue")
                page.get_by_label("License plate", exact=True).fill(f"SYN-{engine}")
                page.get_by_label("VIN", exact=True).fill("SYNTHETIC-PARTIAL")
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
                    self.capture(page, engine, f"vehicle-form-{width}-{height}")
                page.get_by_role("button", name="Add vehicle", exact=True).click()
                title = f"2020 Synthetic Motors Hatchback {engine}"
                expect(
                    page.get_by_role("heading", name=title, exact=True)
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
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"vehicle-detail-{width}-{height}")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "vehicle-detail-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "vehicle-detail-forced-colors")
                    page.emulate_media(forced_colors="none")
                page.emulate_media(color_scheme="light")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 1441
                )
                self.capture(page, engine, "vehicle-detail-200-percent-text")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.get_by_role("link", name="Back to vehicles").click()
                page.get_by_role("searchbox", name="Search vehicles").fill(
                    "no matching vehicle"
                )
                page.get_by_role("button", name="Search", exact=True).click()
                expect(
                    page.get_by_role("heading", name="No matching vehicles")
                ).to_be_visible()
                page.get_by_role("link", name="Clear filters").first.click()
                for width, height in ((1440, 900), (390, 844), (320, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    if width <= 390:
                        self.assertEqual(
                            page.locator(
                                ".vehicle-table td:nth-child(2)"
                            ).first.evaluate(
                                "node => getComputedStyle(node, '::before').content"
                            ),
                            '"License plate: "',
                        )
                    self.capture(page, engine, f"vehicle-list-{width}-{height}")
                page.get_by_role("link", name=title, exact=True).click()
                page.get_by_role("link", name="Edit vehicle").click()
                page.get_by_label("Investigator notes", exact=True).fill(
                    "Reviewed proposal"
                )
                self.db(
                    Vehicle.objects.filter(
                        case=self.case, license_plate=f"SYN-{engine}"
                    ).update,
                    notes="A newer observation",
                    updated_at=self.case.updated_at,
                )
                page.get_by_role("button", name="Save changes", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Review the latest saved details")
                ).to_be_visible()
                expect(
                    page.get_by_label("Investigator notes", exact=True)
                ).to_have_value("Reviewed proposal")
                page.get_by_role("button", name="Save reviewed changes").click()
                expect(
                    page.get_by_text("Reviewed proposal", exact=True)
                ).to_be_visible()

            viewer = self.make_user(f"vehicle-viewer-{engine}")
            CaseMembership.objects.create(case=self.case, user=viewer)
            # Verify ordinary links/forms without JavaScript for a read-only role.
            with self.browser_page(engine, java_script_enabled=False) as (_, page):
                self.sign_in(page, viewer)
                page.goto(self.url("vehicles:list", case_id=self.case.pk))
                expect(page.get_by_role("link", name="Add vehicle")).to_have_count(0)
                page.get_by_role("link", name=title, exact=True).click()
                expect(
                    page.get_by_text("You have viewer access to this vehicle record.")
                ).to_be_visible()
                expect(page.get_by_role("link", name="Edit vehicle")).to_have_count(0)
                expect(page.get_by_role("link", name="Delete vehicle")).to_have_count(0)

            # Owner-only deletion also works without JavaScript and needs confirmation.
            with self.browser_page(engine, java_script_enabled=False) as (_, page):
                self.sign_in(page)
                page.goto(self.url("vehicles:list", case_id=self.case.pk))
                page.get_by_role("link", name="Add vehicle").first.click()
                page.get_by_label("License plate", exact=True).fill(f"DELETE-{engine}")
                page.get_by_role("button", name="Add vehicle", exact=True).click()
                page.get_by_role("link", name="Delete vehicle").click()
                expect(
                    page.get_by_role("heading", name=f"Delete DELETE-{engine}?")
                ).to_be_visible()
                page.get_by_role("checkbox").check()
                page.get_by_role("button", name="Permanently delete vehicle").click()
                expect(
                    page.get_by_role("heading", name="Vehicles", exact=True)
                ).to_be_visible()
                self.assertFalse(
                    self.db(
                        Vehicle.objects.filter(license_plate=f"DELETE-{engine}").exists
                    )
                )

    def test_evidence_admin_journey(self):
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        self.owner.user_permissions.set(
            Permission.objects.filter(content_type__app_label="evidence")
        )
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic document",
            file=SimpleUploadedFile("source.txt", b"Synthetic source bytes."),
        )
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.url("admin:evidence_textevidence_add"))
                page.get_by_label("Case:", exact=True).select_option(str(self.case.pk))
                page.get_by_label("Title:", exact=True).fill(f"Admin source {engine}")
                page.get_by_label("Body:", exact=True).fill("  Synthetic original.\n ")
                page.locator('input[name="_save"]').click()
                source = page.locator("#result_list").get_by_role(
                    "link", name=f"Admin source {engine}", exact=True
                )
                expect(source).to_be_visible()
                source.click()
                expect(page.locator('textarea[name="body"]')).to_have_count(0)
                expect(page.locator('select[name="case"]')).to_have_count(0)
                expect(
                    page.get_by_role("link", name="Delete", exact=True)
                ).to_have_count(0)
                page.get_by_label("Title:", exact=True).focus()
                page.get_by_label("Title:", exact=True).press("Tab")
                expect(page.get_by_label("Description:", exact=True)).to_be_focused()
                page.get_by_label("Is archived", exact=True).check()
                page.locator('input[name="_continue"]').click()
                expect(page.get_by_label("Is archived", exact=True)).to_be_checked()
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    expect(page.get_by_label("Title:", exact=True)).to_be_visible()
                    self.assertLessEqual(
                        page.evaluate(
                            "document.documentElement.scrollWidth - innerWidth"
                        ),
                        1,
                    )
                    self.capture(page, engine, f"evidence-admin-{width}-{height}")
                page.goto(
                    self.live_server_url
                    + reverse(
                        "admin:evidence_documentevidence_change", args=[document.pk]
                    )
                )
                expect(page.get_by_text("source.txt", exact=True)).to_be_visible()
                expect(page.locator('input[type="file"]')).to_have_count(0)
                self.assertNotIn(document.file.name, page.content())
                page.get_by_role("link", name="View on site", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Synthetic document", exact=True)
                ).to_be_visible()
                with page.expect_download() as original:
                    page.locator("header").get_by_role(
                        "link", name="Download original", exact=True
                    ).click()
                self.assertEqual(
                    Path(original.value.path()).read_bytes(), b"Synthetic source bytes."
                )

    def test_location_and_person_location_journey(self):
        person = Person.objects.create(case=self.case, first_name="Alex Example")
        for engine in self.engines:
            for kind, model in (
                ("location", LocationEvidence),
                ("person-location", PersonLocationEvidence),
            ):
                with (
                    self.subTest(engine=engine, kind=kind),
                    self.browser_page(engine, java_script_enabled=False) as (_, page),
                ):
                    self.sign_in(page)
                    page.goto(
                        self.url("evidence:create", case_id=self.case.pk)
                        + f"?kind={kind}"
                    )
                    source = page.get_by_label("Source", exact=True)
                    source.focus()
                    expect(source).to_be_focused()
                    self.assertNotEqual(
                        source.evaluate("node => getComputedStyle(node).outlineStyle"),
                        "none",
                    )
                    page.locator("h1").scroll_into_view_if_needed()
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
                        page.screenshot(
                            path=str(
                                self.artifacts
                                / f"{engine}-{kind}-form-{width}x{height}.png"
                            ),
                            full_page=True,
                        )
                    title = f"Synthetic {kind} {engine}"
                    page.get_by_label("Title", exact=True).fill(title)
                    source.fill("Synthetic harbour witness")
                    if kind == "person-location":
                        page.get_by_label("Person", exact=True).select_option(
                            str(person.pk)
                        )
                    page.get_by_role("button", name="Save location").click()
                    expect(page.locator("#id_title")).to_have_value(title)
                    expect(page.locator("#id_source")).to_have_value(
                        "Synthetic harbour witness"
                    )
                    expect(page.locator(".error-summary")).to_be_visible()
                    page.get_by_label("Address", exact=True).fill(
                        "Synthetic harbour office"
                    )
                    page.get_by_label("Latitude", exact=True).fill("0")
                    page.get_by_label("Longitude", exact=True).fill("-79.123456")
                    page.get_by_label("Observed at (UTC)", exact=True).fill(
                        "2026-01-02T03:04"
                    )
                    page.get_by_role("button", name="Save location").click()
                    expect(
                        page.get_by_role("heading", name=title, exact=True)
                    ).to_be_visible()
                    expect(
                        page.get_by_role("heading", name="Reported location")
                    ).to_be_visible()
                    record = self.db(model.objects.get, case=self.case, title=title)
                    self.assertEqual(record.source, "Synthetic harbour witness")
                    with page.expect_download() as event:
                        page.get_by_role(
                            "link", name="Download original", exact=True
                        ).click()
                    content = Path(event.value.path()).read_bytes()
                    self.assertEqual(
                        event.value.suggested_filename, f"evidence-{record.pk}.json"
                    )
                    self.assertEqual(hashlib.sha256(content).hexdigest(), record.sha256)
                    self.assertEqual(json.loads(content)["latitude"], "0.000000")
                    for width, height in ((1440, 900), (390, 844), (320, 844)):
                        page.set_viewport_size({"width": width, "height": height})
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"),
                            width + 1,
                        )
                        self.capture(page, engine, f"{kind}-original-{width}x{height}")
                    page.get_by_role("link", name="Edit details", exact=True).click()
                    expect(page.locator("#id_address")).to_have_count(0)
                    page.get_by_label("Source", exact=True).fill(
                        "Updated synthetic source"
                    )
                    page.get_by_role("button", name="Save changes", exact=True).click()
                    self.db(record.refresh_from_db)
                    self.assertEqual(record.source, "Updated synthetic source")
                    self.assertEqual(hashlib.sha256(content).hexdigest(), record.sha256)

    def test_management_and_collaboration_journey(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.get_by_role("link", name="Create case", exact=True).first.click()
                name = f"Synthetic browser case {engine}"
                page.get_by_label("Case name", exact=True).fill(name)
                page.get_by_label("Description", exact=True).fill(
                    "Synthetic material for a browser regression journey."
                )
                page.get_by_role("button", name="Create case", exact=True).click()
                expect(
                    page.get_by_role("heading", name=name, exact=True)
                ).to_be_visible()
                case = self.db(Case.objects.get, name=name, owner=self.owner)

                page.goto(self.url("people:create", case_id=case.pk))
                page.get_by_label("First name", exact=True).fill("Alex")
                page.get_by_label("Last name", exact=True).fill("Browser Example")
                page.get_by_label("Investigator notes", exact=True).fill(
                    "Unconfirmed synthetic contact."
                )
                page.get_by_role("button", name="Add person", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Alex Browser Example", exact=True)
                ).to_be_visible()
                page.get_by_role("link", name="Edit person", exact=True).click()
                page.get_by_label("Investigator notes", exact=True).fill(
                    "Updated synthetic source context."
                )
                page.get_by_role("button", name="Save changes", exact=True).click()
                expect(
                    page.get_by_text("Updated synthetic source context.")
                ).to_be_visible()
                self.assertEqual(
                    self.db(Person.objects.get, case=case).notes,
                    "Updated synthetic source context.",
                )

                page.goto(self.url("evidence:create", case_id=case.pk) + "?kind=text")
                title = f"Browser captured source {engine}"
                original = "Synthetic original.\n<untrusted> remains source text."
                page.get_by_label("Title", exact=True).fill(title)
                page.get_by_label("Original text", exact=True).fill(original)
                page.get_by_role("button", name="Save text", exact=True).click()
                expect(
                    page.get_by_role("heading", name=title, exact=True)
                ).to_be_visible()
                expect(page.locator(".original-text")).to_have_text(original)
                record = self.db(TextEvidence.objects.get, case=case, title=title)
                checksum = record.sha256
                page.get_by_role("button", name="Archive", exact=True).click()
                expect(
                    page.get_by_role("button", name="Restore", exact=True)
                ).to_be_visible()
                expect(page.locator(".original-text")).to_have_text(original)
                page.get_by_role("button", name="Restore", exact=True).click()
                expect(
                    page.get_by_role("button", name="Archive", exact=True)
                ).to_be_visible()
                self.db(record.refresh_from_db)
                self.assertFalse(record.is_archived)
                self.assertEqual(record.sha256, checksum)

                page.goto(self.url("evidence:list", case_id=case.pk))
                page.get_by_label("Search evidence", exact=True).fill("captured source")
                page.get_by_role("button", name="Search", exact=True).click()
                expect(page.locator(".evidence-row")).to_have_count(1)
                expect(page.locator(".evidence-row")).to_contain_text(title)

                page.goto(self.url("evidence:create", case_id=case.pk))
                page.locator("#id_file").set_input_files(
                    [
                        {
                            "name": f"synthetic-{index}.txt",
                            "mimeType": "text/plain",
                            "buffer": f"Synthetic batch original {index}.".encode(),
                        }
                        for index in (1, 2)
                    ]
                )
                entries = page.locator(".upload-entry")
                expect(entries).to_have_count(2)
                first_entry = entries.first
                first_entry.get_by_text(
                    "Description and source details", exact=True
                ).click()
                first_entry.get_by_label("Source", exact=True).fill(
                    "Synthetic batch source"
                )
                first_entry.get_by_label("Source URL", exact=True).fill("invalid-url")
                page.get_by_role("button", name="Upload files", exact=True).click()
                expect(
                    page.locator('.upload-entry[data-state="failed"]')
                ).to_have_count(1)
                expect(page.locator('.upload-entry[data-state="added"]')).to_have_count(
                    1
                )
                source_input = first_entry.get_by_label("Source URL", exact=True)
                expect(source_input).to_have_attribute("aria-invalid", "true")
                expect(source_input).to_have_attribute(
                    "aria-describedby",
                    first_entry.locator(".upload-status").get_attribute("id"),
                )
                first_entry.get_by_label("Source URL", exact=True).fill(
                    "https://example.com/synthetic-source"
                )
                first_entry.get_by_role(
                    "button", name="Retry this file", exact=True
                ).click()
                expect(page.locator('.upload-entry[data-state="added"]')).to_have_count(
                    2
                )
                expect(source_input).not_to_have_attribute("aria-invalid", "true")
                self.assertEqual(
                    self.db(DocumentEvidence.objects.filter(case=case).count),
                    2,
                )
                self.assertEqual(
                    self.db(
                        DocumentEvidence.objects.get,
                        case=case,
                        original_filename="synthetic-1.txt",
                    ).source,
                    "Synthetic batch source",
                )
                self.capture(page, engine, "batch-upload-recovery")

                guest = self.db(self.make_user, f"browser-guest-{engine}")
                page.goto(self.url("cases:case_settings", case_id=case.pk))
                page.get_by_role("link", name="Invite collaborator", exact=True).click()
                page.get_by_label("Email address", exact=True).fill(guest.email)
                page.get_by_label("Case access", exact=True).select_option("editor")
                page.get_by_role("button", name="Send invitation", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Case settings")
                ).to_be_visible()
                invitation = self.db(
                    CaseInvitation.objects.get, case=case, email=guest.email
                )
                self.assertFalse(invitation.delivery_failed)
                sent = next(
                    item for item in reversed(mail.outbox) if guest.email in item.to
                )
                token = re.search(r"/invitations/([A-Za-z0-9_-]+)/", sent.body)
                self.assertIsNotNone(token)
                with self.browser_page(engine) as (_, guest_page):
                    self.sign_in(guest_page, guest)
                    guest_page.goto(self.url("invitation_accept", token=token[1]))
                    guest_page.get_by_role(
                        "button", name="Accept invitation", exact=True
                    ).click()
                    expect(
                        guest_page.get_by_role("heading", name=name, exact=True)
                    ).to_be_visible()
                    self.assertEqual(
                        self.db(CaseMembership.objects.get, case=case, user=guest).role,
                        "editor",
                    )
                    page.goto(self.url("cases:case_settings", case_id=case.pk))
                    page.locator(".record-row").filter(
                        has_text=guest.email
                    ).get_by_role("link", name="Change access", exact=True).click()
                    page.locator("select[name='role']").select_option("viewer")
                    page.get_by_role("button", name="Save access", exact=True).click()
                    expect(
                        page.get_by_role("heading", name="Case settings")
                    ).to_be_visible()
                    guest_page.goto(self.evidence_url(record, case=case))
                    expect(
                        guest_page.get_by_role("heading", name=title, exact=True)
                    ).to_be_visible()
                    expect(
                        guest_page.get_by_role("button", name="Archive", exact=True)
                    ).to_have_count(0)
                    expect(
                        guest_page.get_by_role("link", name="Edit details", exact=True)
                    ).to_have_count(0)
                    page.get_by_role("link", name="Remove", exact=True).click()
                    page.get_by_role("checkbox").check()
                    page.get_by_role(
                        "button", name="Remove collaborator", exact=True
                    ).click()
                    expect(
                        page.get_by_role("heading", name="Case settings")
                    ).to_be_visible()
                    response = guest_page.goto(self.evidence_url(record, case=case))
                    self.assertEqual(response.status, 404)
                    self.assertFalse(
                        self.db(
                            CaseMembership.objects.filter(case=case, user=guest).exists
                        )
                    )
                self.capture(page, engine, "collaboration-settings")

    def test_file_upload_and_recovery_without_javascript(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("evidence:bulk_create", case_id=self.case.pk))
                expect(page.locator("[data-bulk-fallback]")).to_be_visible()
                expect(page.locator("[data-bulk-upload]")).to_be_hidden()
                page.get_by_role("link", name="add one file at a time").click()
                page.get_by_label("Title", exact=True).fill("Keep this proposed title")
                page.get_by_role("button", name="Upload file", exact=True).click()
                expect(page.locator("#id_title")).to_have_value(
                    "Keep this proposed title"
                )
                expect(page.get_by_role("heading", name="Add evidence")).to_be_visible()
                content = b"Synthetic source file.\r\nPreserve these original bytes.\n"
                title = f"No-JavaScript document {engine}"
                page.get_by_label("Title", exact=True).fill(title)
                page.locator("#id_file").set_input_files(
                    {
                        "name": "synthetic-source.txt",
                        "mimeType": "text/plain",
                        "buffer": content,
                    }
                )
                page.get_by_role("button", name="Upload file", exact=True).click()
                expect(
                    page.get_by_role("heading", name=title, exact=True)
                ).to_be_visible()
                with page.expect_download() as event:
                    page.get_by_role(
                        "link", name="Download original", exact=True
                    ).first.click()
                download = event.value
                self.assertEqual(Path(download.path()).read_bytes(), content)
                document = self.db(
                    DocumentEvidence.objects.get, case=self.case, title=title
                )
                self.assertEqual(document.byte_size, len(content))
                self.capture(page, engine, "no-javascript-document")

                record = self.records[-1]
                page.goto(self.evidence_url(record))
                with page.expect_download() as text_original:
                    page.get_by_role("link", name="Download text", exact=True).click()
                self.assertEqual(
                    text_original.value.suggested_filename, f"evidence-{record.pk}.txt"
                )
                self.assertEqual(
                    Path(text_original.value.path()).read_bytes(),
                    record.body.encode("utf-8"),
                )

    def test_uncertain_commit_retry_and_proxy_size_limit_recovery(self):
        original = b"Synthetic original with interrupted confirmation."
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                upload_url = self.url("evidence:create", case_id=self.case.pk)
                bulk_url = self.url("evidence:bulk_create", case_id=self.case.pk)
                page.goto(bulk_url)
                page.locator(".bulk-shared-details>summary").click()
                page.locator("#id_source").fill("Original shared source")
                page.locator("#id_file").set_input_files(
                    {
                        "name": f"interrupted-{engine}.txt",
                        "mimeType": "text/plain",
                        "buffer": original,
                    }
                )
                entry = page.locator(".upload-entry")
                expect(entry).to_have_count(1)
                title = f"Interrupted confirmation {engine}"
                entry.get_by_label("Title", exact=True).fill(title)
                files_before = {
                    path.relative_to(self.media_directory.name).as_posix()
                    for path in Path(self.media_directory.name).rglob("*")
                    if path.is_file()
                }
                self.interrupted_statuses = []
                self.interrupted_details = []

                def commit_then_abort(route):
                    # Fetch reaches the real view and commits its transaction;
                    # only the browser's confirmation is deliberately lost.
                    body = route.request.post_data_buffer or b""
                    headers = route.request.headers.copy()
                    if original not in body:
                        # macOS WebKit omits file bytes from routed multipart
                        # request bodies. Preserve the captured fields (including
                        # the actual queue submission UUID) and authentication/
                        # CSRF headers, restoring only the same selected fixture
                        # file through Django's standard multipart encoder.
                        message = BytesParser(policy=policy.default).parsebytes(
                            (
                                f"Content-Type: {headers['content-type']}\r\n"
                                "MIME-Version: 1.0\r\n\r\n"
                            ).encode("ascii")
                            + body
                        )
                        fields = {}
                        for part in message.iter_parts():
                            name = part.get_param("name", header="content-disposition")
                            filename = part.get_filename()
                            if filename is not None:
                                fields[name] = SimpleUploadedFile(
                                    filename,
                                    original,
                                    content_type=part.get_content_type(),
                                )
                            else:
                                fields[name] = part.get_payload(decode=True).decode(
                                    "utf-8"
                                )
                        body = encode_multipart(BOUNDARY, fields)
                        headers.pop("content-length", None)
                        headers["content-type"] = MULTIPART_CONTENT
                    response = route.fetch(
                        headers=headers, post_data=body, timeout=10000
                    )
                    self.interrupted_statuses.append(response.status)
                    if response.status != 201:
                        self.interrupted_details.append(
                            {
                                "response": response.json(),
                                "request_bytes": len(
                                    route.request.post_data_buffer or b""
                                ),
                                "has_original": (
                                    b"Synthetic original with interrupted confirmation."
                                    in (route.request.post_data_buffer or b"")
                                ),
                            }
                        )
                    route.abort("failed")

                page.route(upload_url, commit_then_abort)
                page.get_by_role("button", name="Upload file", exact=True).click()
                expect(entry).to_have_attribute("data-state", "unknown", timeout=15000)
                expect(entry.locator(".upload-status")).to_contain_text("interrupted")
                expect(entry.get_by_label("Title", exact=True)).to_be_disabled()
                expect(page.locator("#id_file")).to_be_disabled()
                page.locator("#id_source").fill("Later shared source")
                expect(entry.locator('[data-field-name="source"]')).to_have_value(
                    "Original shared source"
                )
                expect(
                    entry.get_by_role(
                        "button",
                        name="Remove from queue",
                        exact=True,
                        include_hidden=True,
                    )
                ).to_be_hidden()
                self.assertEqual(
                    self.interrupted_statuses, [201], self.interrupted_details
                )
                committed = self.db(
                    DocumentEvidence.objects.get, case=self.case, title=title
                )
                committed_id = committed.pk
                submission_id = committed.submission_id
                original_key = committed.file.name
                original_checksum = committed.sha256
                files_after_commit = {
                    path.relative_to(self.media_directory.name).as_posix()
                    for path in Path(self.media_directory.name).rglob("*")
                    if path.is_file()
                }
                self.assertEqual(len(files_after_commit - files_before), 1)
                self.capture(page, engine, "upload-confirmation-interrupted")

                page.unroute(upload_url, commit_then_abort)
                entry.get_by_role("button", name="Retry this file", exact=True).click()
                expect(entry).to_have_attribute("data-state", "added")
                self.assertEqual(
                    self.db(
                        DocumentEvidence.objects.filter(
                            case=self.case, title=title
                        ).count
                    ),
                    1,
                )
                retried = self.db(
                    DocumentEvidence.objects.get, case=self.case, title=title
                )
                self.assertEqual(retried.pk, committed_id)
                self.assertEqual(retried.submission_id, submission_id)
                self.assertEqual(retried.file.name, original_key)
                self.assertEqual(retried.sha256, original_checksum)
                self.assertEqual(retried.source, "Original shared source")
                self.assertEqual(
                    {
                        path.relative_to(self.media_directory.name).as_posix()
                        for path in Path(self.media_directory.name).rglob("*")
                        if path.is_file()
                    },
                    files_after_commit,
                )

                page.goto(bulk_url)
                page.locator("#id_file").set_input_files(
                    {
                        "name": f"proxy-rejected-{engine}.txt",
                        "mimeType": "text/plain",
                        "buffer": b"Synthetic original rejected before Django.",
                    }
                )
                entry = page.locator(".upload-entry")
                proxy_title = f"Proxy rejected original {engine}"
                entry.get_by_label("Title", exact=True).fill(proxy_title)

                def reject_at_proxy(route):
                    route.fulfill(
                        status=413,
                        content_type="text/html",
                        body="<html><body>Request body too large</body></html>",
                    )

                page.route(upload_url, reject_at_proxy)
                page.get_by_role("button", name="Upload file", exact=True).click()
                expect(entry).to_have_attribute("data-state", "failed")
                expect(entry.locator(".upload-status")).to_contain_text(
                    "server's upload limit"
                )
                expect(entry.get_by_label("Title", exact=True)).to_be_enabled()
                expect(
                    entry.get_by_role("button", name="Retry this file", exact=True)
                ).to_be_enabled()
                self.assertFalse(
                    self.db(
                        DocumentEvidence.objects.filter(
                            case=self.case, title=proxy_title
                        ).exists
                    )
                )
                self.capture(page, engine, "proxy-upload-limit")
                page.unroute(upload_url, reject_at_proxy)
                reviewed_title = f"Reviewed proxy rejection {engine}"
                entry.get_by_label("Title", exact=True).fill(reviewed_title)
                entry.get_by_role("button", name="Retry this file", exact=True).click()
                expect(entry).to_have_attribute("data-state", "added")
                self.assertEqual(
                    self.db(
                        DocumentEvidence.objects.filter(
                            case=self.case, title=reviewed_title
                        ).count
                    ),
                    1,
                )

    @override_settings(PANDECT_MAX_BATCH_FILES=4, PANDECT_MAX_FILE_BYTES=128)
    def test_bulk_files_shared_details_append_pause_and_validation(self):
        originals = [
            {"name": "report.txt", "mimeType": "text/plain", "buffer": b"Report bytes"},
            {"name": "photo.png", "mimeType": "image/png", "buffer": b"Image bytes"},
            {"name": "sound.wav", "mimeType": "audio/wav", "buffer": b"Audio bytes"},
        ]
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                files = [
                    {**item, "name": f"{engine}-{item['name']}"} for item in originals
                ]
                page.goto(self.url("cases:case_detail", case_id=self.case.pk))
                page.get_by_role("link", name="Bulk add files", exact=True).click()
                expect(page.locator("[data-bulk-fallback]")).to_be_hidden()
                shared = page.locator("[data-shared-details]")
                page.locator(".bulk-shared-details>summary").click()
                shared.get_by_label("Source", exact=True).fill("Batch source")
                shared.get_by_label("Source URL", exact=True).fill(
                    "https://example.com/source"
                )
                shared.get_by_label("Source description", exact=True).fill(
                    "Synthetic collection"
                )
                shared.get_by_label("Source collected at (UTC)").fill(
                    "2026-10-26T12:30"
                )
                picker = page.locator("#id_file")
                picker.set_input_files(files[:2])
                entries = page.locator(".upload-entry")
                expect(entries).to_have_count(2)
                first, second = entries.nth(0), entries.nth(1)
                first.get_by_text("Description and source details", exact=True).click()
                first.get_by_label("Source", exact=True).fill("Individual witness")
                second.get_by_text("Description and source details", exact=True).click()
                second.get_by_label("Source", exact=True).fill("")
                second.get_by_label("Source URL", exact=True).fill("invalid-url")
                picker.set_input_files(files[2])
                expect(entries).to_have_count(3)
                third = entries.nth(2)
                third.get_by_text("Description and source details", exact=True).click()
                third.get_by_label("Source", exact=True).fill("Temporary override")
                third.get_by_role("button", name="Use shared details").click()
                shared.get_by_label("Source", exact=True).fill("Reviewed batch source")
                expect(first.get_by_label("Source", exact=True)).to_have_value(
                    "Individual witness"
                )
                expect(second.get_by_label("Source", exact=True)).to_have_value("")
                expect(third.get_by_label("Source", exact=True)).to_have_value(
                    "Reviewed batch source"
                )
                for index, category in enumerate(("document", "image", "audio")):
                    expect(entries.nth(index).get_by_label("Category")).to_have_value(
                        category
                    )
                    expect(entries.nth(index).get_by_label("Title")).to_have_value(
                        files[index]["name"]
                    )
                picker.set_input_files(files[:2])
                expect(entries).to_have_count(3)
                expect(page.locator("[data-upload-status]")).to_contain_text(
                    "room for 1 more file"
                )
                page.locator(".bulk-shared-details>summary").click()
                for details in entries.locator("details").all():
                    details.locator("summary").click()
                title = first.get_by_label("Title", exact=True)
                title.focus()
                title.press(
                    "Alt+Tab"
                    if engine == "webkit" and sys.platform == "darwin"
                    else "Tab"
                )
                expect(first.get_by_label("Category")).to_be_focused()
                self.assertNotEqual(
                    first.get_by_label("Category").evaluate(
                        "node => getComputedStyle(node).outlineStyle"
                    ),
                    "none",
                )
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("window.scrollTo(0, 0)")
                    self.assertLessEqual(
                        page.evaluate(
                            "document.documentElement.scrollWidth - innerWidth"
                        ),
                        1,
                    )
                    self.capture(page, engine, f"bulk-files-{width}-{height}")
                    if width == 390:
                        page.screenshot(
                            path=str(self.artifacts / f"{engine}-bulk-files-full.png"),
                            full_page=True,
                        )
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "bulk-files-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "bulk-files-forced-colors")
                    page.emulate_media(forced_colors="none")
                page.emulate_media(color_scheme="light")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 1441
                )
                self.capture(page, engine, "bulk-files-200-percent-text")
                page.evaluate("document.documentElement.style.fontSize = ''")
                upload_url = self.url("evidence:create", case_id=self.case.pk)

                def pause_first(route, *, picker=picker, shared=shared):
                    expect(picker).to_be_disabled()
                    self.assertTrue(shared.evaluate("node => node.disabled"))
                    page.get_by_role("button", name="Stop after this file").click()
                    route.continue_()

                page.route(upload_url, pause_first, times=1)
                page.get_by_role("button", name="Upload files", exact=True).click()
                expect(first).to_have_attribute("data-state", "added")
                expect(page.locator("[data-upload-status]")).to_contain_text("paused")
                expect(second).to_have_attribute("data-state", "ready")
                page.locator(".bulk-shared-details>summary").click()
                shared.get_by_label("Source", exact=True).fill("After first upload")
                expect(first.locator('[data-field-name="source"]')).to_have_value(
                    "Individual witness"
                )
                expect(third.locator('[data-field-name="source"]')).to_have_value(
                    "After first upload"
                )
                page.get_by_role("button", name="Upload files", exact=True).click()
                expect(second).to_have_attribute("data-state", "failed")
                expect(third).to_have_attribute("data-state", "added")
                second.get_by_text("Description and source details", exact=True).click()
                second.get_by_label("Source URL", exact=True).fill(
                    "https://example.com/corrected"
                )
                second.get_by_role("button", name="Retry this file").click()
                expect(second).to_have_attribute("data-state", "added")
                expect(
                    page.get_by_role("link", name="Start another batch")
                ).to_be_visible()
                # Another deliberate selection of the same filename is a new record.
                picker.set_input_files(files[0])
                expect(entries).to_have_count(4)
                expect(
                    page.get_by_role("link", name="Start another batch")
                ).to_be_hidden()
                page.get_by_role("button", name="Upload files", exact=True).click()
                expect(entries.nth(3)).to_have_attribute("data-state", "added")
                expect(page.locator("[data-upload-status]")).to_contain_text(
                    "4 of 4 files added"
                )
                for model, filename, source, original in (
                    (
                        DocumentEvidence,
                        files[0]["name"],
                        "Individual witness",
                        files[0],
                    ),
                    (ImageEvidence, files[1]["name"], "", files[1]),
                    (AudioEvidence, files[2]["name"], "After first upload", files[2]),
                ):
                    record = self.db(
                        model.objects.get,
                        case=self.case,
                        original_filename=filename,
                        source=source,
                    )
                    self.assertEqual(
                        record.sha256, hashlib.sha256(original["buffer"]).hexdigest()
                    )
                    self.assertEqual(record.uploaded_by_id, self.owner.pk)
                    self.assertEqual(
                        record.collected_at.isoformat(), "2026-10-26T12:30:00+00:00"
                    )
                    self.assertEqual(
                        Path(record.file.path).read_bytes(), original["buffer"]
                    )
                page.get_by_role("link", name="Start another batch").click()
                expect(page.locator(".upload-entry")).to_have_count(0)
                expect(page.get_by_role("button", name="Upload files")).to_be_disabled()

    @override_settings(PANDECT_MAX_FILE_BYTES=16)
    def test_bulk_invalid_files_removal_and_access_removed_between_uploads(self):
        for engine in self.engines:
            editor = self.make_user(f"bulk-editor-{engine}")
            membership = CaseMembership.objects.create(
                case=self.case, user=editor, role="editor"
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, editor)
                page.goto(self.url("evidence:bulk_create", case_id=self.case.pk))
                picker = page.locator("#id_file")
                picker.set_input_files(
                    [
                        {"name": "empty.txt", "mimeType": "text/plain", "buffer": b""},
                        {
                            "name": "large.txt",
                            "mimeType": "text/plain",
                            "buffer": b"x" * 17,
                        },
                    ]
                )
                entries = page.locator(".upload-entry")
                expect(
                    page.locator('.upload-entry[data-state="failed"]')
                ).to_have_count(2)
                expect(page.get_by_role("button", name="Upload files")).to_be_disabled()
                expect(
                    page.get_by_role("button", name="Retry this file")
                ).to_have_count(0)
                for _ in range(2):
                    entries.first.get_by_role(
                        "button", name="Remove from queue"
                    ).click()
                expect(entries).to_have_count(0)
                picker.set_input_files(
                    [
                        {
                            "name": f"{engine}-{i}.txt",
                            "mimeType": "text/plain",
                            "buffer": b"Synthetic bytes",
                        }
                        for i in range(3)
                    ]
                )

                def pause_first(route):
                    page.get_by_role("button", name="Stop after this file").click()
                    route.continue_()

                page.route(
                    self.url("evidence:create", case_id=self.case.pk),
                    pause_first,
                    times=1,
                )
                page.get_by_role("button", name="Upload files").click()
                expect(entries.first).to_have_attribute("data-state", "added")
                expect(page.locator("[data-upload-status]")).to_contain_text("paused")
                self.db(membership.delete)
                page.get_by_role("button", name="Upload files").click()
                expect(entries.nth(1)).to_have_attribute("data-state", "blocked")
                expect(entries.nth(2)).to_have_attribute("data-state", "ready")
                expect(picker).to_be_disabled()
                expect(page.get_by_role("button", name="Upload files")).to_be_disabled()
                expect(page.locator("[data-upload-status]")).to_contain_text(
                    "access is unavailable"
                )
                self.assertEqual(
                    self.db(DocumentEvidence.objects.filter(uploaded_by=editor).count),
                    1,
                )

    def test_dense_responsive_workspace_keyboard_and_appearance(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.evidence_url())
                expect(page.locator(".evidence-row")).to_have_count(25)
                for height, minimum in ((900, 12), (1024, 14)):
                    page.set_viewport_size({"width": 1440, "height": height})
                    visible = page.locator(".evidence-row").evaluate_all(
                        """rows => {
                          const pane = document.querySelector('.evidence-records')
                            .getBoundingClientRect();
                          const top = Math.max(0, pane.top);
                          const bottom = Math.min(innerHeight, pane.bottom);
                          return rows.filter(row => {
                            const box = row.getBoundingClientRect();
                            return box.height >= 44 && box.top >= top - 0.5
                              && box.bottom <= bottom + 0.5;
                          }).length;
                        }"""
                    )
                    self.assertGreaterEqual(
                        visible, minimum, f"{engine}: density at 1440x{height}"
                    )
                    self.assertGreaterEqual(
                        page.locator("body").evaluate(
                            "node => parseFloat(getComputedStyle(node).fontSize)"
                        ),
                        16,
                    )
                    self.capture(page, engine, f"workspace-1440x{height}")

                first = page.locator(".evidence-row").first
                second = page.locator(".evidence-row").nth(1)
                title = second.locator(".evidence-row-title").inner_text()
                first.focus()
                expect(first).to_be_focused()
                # Safari on macOS uses Option-Tab for links unless its advanced
                # keyboard preference is changed; Linux WebKit uses ordinary Tab.
                # https://support.apple.com/guide/safari/cpsh003/mac
                first.press(
                    "Alt+Tab"
                    if engine == "webkit" and sys.platform == "darwin"
                    else "Tab"
                )
                expect(second).to_be_focused()
                self.assertNotEqual(
                    second.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                second.press("Enter")
                expect(
                    page.get_by_role("heading", name=title, exact=True)
                ).to_be_visible()

                light = page.locator("body").evaluate(
                    "node => getComputedStyle(node).backgroundColor"
                )
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.assertNotEqual(
                    page.locator("body").evaluate(
                        "node => getComputedStyle(node).backgroundColor"
                    ),
                    light,
                )
                self.assertTrue(
                    page.evaluate(
                        "matchMedia('(prefers-reduced-motion: reduce)').matches"
                    )
                )
                self.capture(page, engine, "workspace-dark-reduced-motion")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "workspace-forced-colors")
                    page.emulate_media(forced_colors="none")
                page.emulate_media(color_scheme="light", reduced_motion="no-preference")

                for width, height in ((768, 1024), (390, 844), (320, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(self.evidence_url())
                    expect(
                        page.get_by_role("link", name="Back to evidence")
                    ).to_be_visible()
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"),
                        width + 1,
                        f"{engine}: detail page overflow at {width}px",
                    )
                    self.capture(page, engine, f"detail-{width}x{height}")
                    page.get_by_role("link", name="Back to evidence").click()
                    expect(page.locator(".evidence-row").first).to_be_visible()
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    if width == 390:
                        self.capture(page, engine, "list-390x844")
                    page.locator(".evidence-row").first.click()
                    expect(page.locator(".original-text")).to_be_visible()

                # Emulate 200% text sizing independently of device pixel density.
                # Unitless row line heights must grow with the enlarged text.
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(self.evidence_url())
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 1441
                )
                title_box = page.locator(".evidence-row-title").first.bounding_box()
                meta_box = page.locator(".evidence-row-meta").first.bounding_box()
                self.assertGreaterEqual(
                    meta_box["y"], title_box["y"] + title_box["height"] - 1
                )
                self.capture(page, engine, "workspace-200-percent-text")

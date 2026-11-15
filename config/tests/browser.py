"""Explicit browser journeys: python manage.py test config.tests.browser.

This module is intentionally outside default ``test*.py`` discovery. Install the
Playwright browsers first; use PANDECT_BROWSER_ENGINES to select engines and
PANDECT_BROWSER_ARTIFACTS to choose the synthetic screenshot directory.
"""

import hashlib
import json
import re
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

from django.contrib.auth.models import Permission
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse
from playwright.sync_api import expect

from audio.models import AudioEvidence
from cases.models import Case, CaseInvitation, CaseMembership
from config.tests.browser_base import BrowserTestCase
from document.models import DocumentEvidence
from geo.models import (
    LocationEvidence,
    PersonLocationEvidence,
    VehicleLocationEvidence,
    VehicleLocationImport,
)
from geo.testing import csv_content, parse_csv
from geo.vehicle_import_jobs import enqueue_preparation, process_import
from image.models import ImageEvidence
from people.models import Person
from text.models import TextEvidence
from vehicle.models import Vehicle


class CoreInterfaceBrowserTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
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

    def test_shared_workspace_navigation_and_icon_tooltips(self):
        Person.objects.create(case=self.case, first_name="Synthetic contact")
        Vehicle.objects.create(
            case=self.case, make="Synthetic van", license_plate="TEST"
        )
        screens = (
            ("cases:case_list", {}, "cases"),
            ("people:list", {"case_id": self.case.pk}, "people"),
            ("vehicle:list", {"case_id": self.case.pk}, "vehicles"),
            ("core:analysis:index", {"case_id": self.case.pk}, "findings"),
            ("cases:case_settings", {"case_id": self.case.pk}, "settings"),
            ("account_email", {}, "account"),
        )
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                for route, kwargs, name in screens:
                    page.goto(self.url(route, **kwargs))
                    expect(page.locator(".app-navigation")).to_have_count(1)
                    if kwargs:
                        expect(page.locator(".case-context")).to_have_text(
                            "Case " + self.case.name
                        )
                    for width, height in (
                        (1440, 900),
                        (1440, 1024),
                        (768, 1024),
                        (390, 844),
                        (320, 844),
                    ):
                        page.set_viewport_size({"width": width, "height": height})
                        page.evaluate("new Promise(requestAnimationFrame)")
                        control = page.get_by_role("link", name="All cases", exact=True)
                        control.evaluate("node => node.blur()")
                        control.focus()
                        tip = page.get_by_role("tooltip")
                        expect(tip).to_have_text("All cases")
                        expect(tip).to_be_visible()
                        box = tip.bounding_box()
                        self.assertGreaterEqual(box["x"], 0)
                        self.assertLessEqual(box["x"] + box["width"], width)
                        self.assertGreaterEqual(box["y"], 0)
                        self.assertLessEqual(box["y"] + box["height"], height)
                        control.press("Escape")
                        expect(tip).to_be_hidden()
                        expect(control).to_be_focused()
                        expect(control).not_to_have_attribute("aria-describedby")
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"),
                            width + 1,
                        )
                        self.capture(page, engine, f"shared-{name}-{width}x{height}")
                    page.locator("h1").evaluate(
                        "node => { node.tabIndex = -1; node.focus(); }"
                    )
                    control.hover()
                    expect(tip).to_be_visible()
                    tip.hover()
                    expect(tip).to_be_visible()
                    page.mouse.move(310, 800)
                    expect(tip).to_be_hidden()
                    if name in {"people", "vehicles"}:
                        row = page.locator(".collection-row").first
                        self.assertGreaterEqual(row.bounding_box()["height"], 56)
                        expect(row.locator(".collection-row-meta")).to_be_visible()
                    if kwargs:
                        tools = page.locator(".case-tools")
                        tools.locator(":scope > summary").click()
                        expect(
                            tools.get_by_role("link", name="Vehicles")
                        ).to_be_visible()
                        close = tools.get_by_role("button", name="Close case tools")
                        close.focus()
                        expect(tip).to_have_text("Close case tools")
                        close.press("Escape")
                        expect(tip).to_be_hidden()
                        expect(tools).to_have_attribute("open", "")
                        close.press("Escape")
                        expect(tools).not_to_have_attribute("open", "")
                        expect(tools.locator(":scope > summary")).to_be_focused()
                    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                    self.capture(page, engine, f"shared-{name}-dark-mobile")
                    if engine == "chromium":
                        page.emulate_media(forced_colors="active")
                        control.focus()
                        expect(tip).to_be_visible()
                        self.capture(page, engine, f"shared-{name}-forced-colors")
                        page.emulate_media(forced_colors="none")
                    page.set_viewport_size({"width": 1440, "height": 900})
                    page.evaluate("document.documentElement.style.fontSize = '200%'")
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), 1441
                    )
                    for icon in page.locator(".app-navigation .icon").all():
                        if icon.is_visible():
                            self.assertGreaterEqual(icon.bounding_box()["width"], 24)
                    self.capture(page, engine, f"shared-{name}-200-percent-text")
                    page.emulate_media(color_scheme="light")

                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(self.evidence_url())
                for name in ("Info", "More"):
                    control = page.locator(f".evidence-{name.lower()} > summary")
                    control.focus()
                    expect(page.get_by_role("tooltip")).to_have_text(name)
                    self.capture(
                        page, engine, f"shared-evidence-tooltip-{name.lower()}"
                    )
                    control.press("Escape")
                page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                page.locator(".evidence-filters > summary").focus()
                expect(page.get_by_role("tooltip")).to_have_text("Filters")

    def test_image_evidence_preview_and_download(self):
        import io

        from PIL import Image, ImageDraw

        buffer = io.BytesIO()
        with Image.new("RGB", (1280, 720), "#dce9ef") as image:
            drawing = ImageDraw.Draw(image)
            drawing.rectangle((0, 390, 1280, 720), fill="#497d96")
            drawing.rectangle((160, 220, 700, 390), fill="#caad8a")
            drawing.polygon(((120, 220), (430, 80), (740, 220)), fill="#775c54")
            drawing.rectangle((360, 270, 470, 390), fill="#584d4a")
            drawing.rectangle((820, 440, 1180, 470), fill="#dcc8a6")
            drawing.line((900, 390, 900, 580), fill="#655449", width=12)
            drawing.line((1100, 390, 1100, 580), fill="#655449", width=12)
            with Image.new("RGB", (32, 24), "gray") as auxiliary:
                image.save(buffer, format="MPO", append_images=[auxiliary])
        original = buffer.getvalue()
        retained = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic harbour photograph",
            source="Synthetic interface fixture",
            description="Illustration used to validate the image evidence preview.",
            file=SimpleUploadedFile(
                "harbour.jpeg", original, content_type="image/jpeg"
            ),
        )
        unavailable = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Unsupported image",
            file=SimpleUploadedFile("unsupported.svg", b"<svg/>", "image/svg+xml"),
        )
        url = self.url(
            "core:evidence:detail",
            case_id=self.case.pk,
            kind="image",
            evidence_id=retained.pk,
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine, javascript=False),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(url)
                preview = page.get_by_role(
                    "img", name="Image preview of Synthetic harbour photograph"
                )
                expect(preview).to_be_visible()
                # Playwright can inspect DOM properties even with page scripts disabled.
                self.assertTrue(
                    preview.evaluate("img => img.complete && img.naturalWidth > 0")
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
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.assertLessEqual(preview.bounding_box()["width"], width)
                    page.screenshot(
                        path=str(
                            self.artifacts
                            / f"{engine}-image-preview-{width}x{height}.png"
                        ),
                        full_page=width < 1280,
                        animations="disabled",
                    )
                page.set_viewport_size({"width": 1440, "height": 900})
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 1441
                )
                self.capture(page, engine, "image-preview-200-percent-text")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "image-preview-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "image-preview-forced-colors")
                    page.emulate_media(forced_colors="none")
                download = page.get_by_role(
                    "link", name="Download original", exact=True
                ).first
                download.focus()
                expect(download).to_be_focused()
                self.assertNotEqual(
                    download.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                with page.expect_download() as saved:
                    download.press("Enter")
                self.assertEqual(Path(saved.value.path()).read_bytes(), original)

            with (
                self.subTest(engine=engine, javascript=True),
                self.browser_page(engine) as (_, page),
            ):
                self.sign_in(page)
                page.goto(url)
                expect(page.locator("#image-preview-caption")).to_have_text(
                    "Resized preview. Download the original for full detail."
                )
                page.goto(
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="image",
                        evidence_id=unavailable.pk,
                    )
                )
                expect(page.locator("#image-preview-caption")).to_have_text(
                    "Image preview unavailable. Download the original to inspect it."
                )
                expect(page.locator("[data-image-preview] img")).to_be_hidden()
                expect(
                    page.get_by_role("link", name="Download original", exact=True).first
                ).to_be_visible()
                self.capture(page, engine, "image-preview-unavailable")

    def test_page_layouts_and_evidence_type_selection_without_javascript(self):
        person = Person.objects.create(case=self.case, first_name="Synthetic contact")
        vehicle = Vehicle.objects.create(case=self.case, make="Synthetic vehicle")
        screens = (
            ("cases:case_list", {}, "cases"),
            ("core:evidence:list", {"case_id": self.case.pk}, "evidence-landing"),
            ("cases:case_settings", {"case_id": self.case.pk}, "settings"),
            ("people:list", {"case_id": self.case.pk}, "people"),
            (
                "people:detail",
                {"case_id": self.case.pk, "person_id": person.pk},
                "person",
            ),
            ("vehicle:list", {"case_id": self.case.pk}, "vehicles"),
            (
                "vehicle:detail",
                {"case_id": self.case.pk, "vehicle_id": vehicle.pk},
                "vehicle",
            ),
            ("account_email", {}, "account"),
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                for route, kwargs, name in screens:
                    page.goto(self.url(route, **kwargs))
                    for control in page.locator("[data-tooltip]").all():
                        self.assertTrue(control.get_attribute("title"))
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
                            f"{engine}: {name} overflow at {width}px",
                        )
                        self.capture(page, engine, f"refined-{name}-{width}x{height}")

                page.goto(self.url("core:evidence:create", case_id=self.case.pk))
                expect(
                    page.get_by_role("heading", name="Add evidence", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role(
                        "link", name="Import vehicle locations", exact=True
                    )
                ).to_be_visible()
                more = page.get_by_text("More ways to add evidence", exact=True)
                expect(
                    page.get_by_role("link", name="Add location", exact=True)
                ).to_be_hidden()
                more.press("Enter")
                expect(
                    page.get_by_role("link", name="Add location", exact=True)
                ).to_be_visible()
                page.get_by_role("link", name="Add text", exact=True).press("Enter")
                expect(page.get_by_label("Original text", exact=True)).to_be_visible()
                for kind, label in (
                    ("text", "Original text"),
                    ("location", "Address"),
                    ("phone-call", "Sender"),
                ):
                    expect(
                        page.get_by_label("Evidence type", exact=True)
                    ).to_be_hidden()
                    page.get_by_text("Change type", exact=True).press("Enter")
                    page.get_by_label("Evidence type", exact=True).select_option(kind)
                    page.get_by_role("button", name="Choose type", exact=True).click()
                    expect(page.get_by_label(label, exact=True)).to_be_visible()
                    self.assertIn(f"kind={kind}", page.url)
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), 321
                    )
                    self.capture(page, engine, f"refined-create-{kind}-320x844")

    def test_evidence_disclosures_preserve_keyboard_and_error_recovery(self):
        self.case.description += " Historical case context." * 8
        self.case.save(update_fields=["description"])
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                page.set_viewport_size({"width": 390, "height": 844})
                navigation = page.get_by_role("navigation", name="Investigation")
                expect(navigation).to_have_count(1)
                expect(
                    navigation.get_by_role("link", name="Evidence", exact=True)
                ).to_have_attribute("aria-current", "page")
                case_tools = navigation.locator(".case-tools")
                tools_summary = case_tools.locator(":scope > summary")
                map_link = case_tools.get_by_role("link", name="Map", exact=True)
                expect(map_link).to_be_hidden()
                tools_summary.focus()
                tools_summary.press("Enter")
                expect(map_link).to_be_visible()
                case_details = case_tools.locator(".case-details-body")
                expect(
                    case_details.get_by_text(self.case.description, exact=True)
                ).to_be_visible()
                expect(
                    case_details.get_by_text(self.owner.username, exact=True)
                ).to_be_visible()
                expect(case_details.locator("time")).to_have_count(2)
                expect(case_details.locator("input, textarea")).to_have_count(0)
                self.capture(page, engine, "case-tools-390x844")
                tools_summary.press("Enter")
                expect(map_link).to_be_hidden()
                page.set_viewport_size({"width": 1440, "height": 900})
                # The same compact control remains available after resizing.
                expect(tools_summary).to_be_visible()
                filters = page.locator(".evidence-filters")
                expect(page.get_by_label("State", exact=True)).to_be_hidden()
                filters.locator("summary").focus()
                filters.locator("summary").press("Enter")
                page.get_by_label("State", exact=True).select_option("all")
                page.get_by_role("button", name="Apply filters", exact=True).click()
                expect(page.locator(".evidence-filters")).to_have_attribute("open", "")
                expect(page.get_by_label("State", exact=True)).to_have_value("all")
                page.locator(".evidence-filters > summary").press("Enter")
                page.locator(".evidence-row").first.click()
                self.assertIn("state=all", page.url)

                page.goto(
                    self.url("core:evidence:create", case_id=self.case.pk)
                    + "?kind=text"
                )
                extra = page.locator("[data-additional-evidence-details]")
                expect(page.get_by_label("Description", exact=True)).to_have_count(0)
                expect(page.get_by_label("Source", exact=True)).to_be_hidden()
                expect(page.get_by_label("Source URL", exact=True)).to_be_hidden()
                summary = extra.locator("summary")
                summary.focus()
                summary.press("Enter")
                expect(page.get_by_label("Source URL", exact=True)).to_be_visible()
                page.get_by_label("Source description", exact=True).fill(
                    "Synthetic source context"
                )
                page.get_by_label("Source", exact=True).fill("Synthetic archive")
                page.get_by_label("Title", exact=True).fill(
                    f"Disclosure source {engine}"
                )
                page.get_by_label("Original text", exact=True).fill(
                    f"Synthetic original {engine}"
                )
                page.get_by_label("Source URL", exact=True).fill("invalid-url")
                # Exercise server recovery even when browser URL validation is bypassed.
                page.locator("#evidence-create-form").evaluate(
                    "form => form.noValidate = true"
                )
                page.get_by_role("button", name="Save text", exact=True).click()
                expect(page.locator(".error-summary")).to_be_visible()
                expect(extra).to_have_attribute("open", "")
                expect(
                    page.get_by_label("Source description", exact=True)
                ).to_have_value("Synthetic source context")
                expect(page.get_by_label("Source", exact=True)).to_have_value(
                    "Synthetic archive"
                )
                expect(page.get_by_label("Source URL", exact=True)).to_have_value(
                    "invalid-url"
                )
                page.get_by_label("Source URL", exact=True).fill(
                    "https://example.com/source"
                )
                page.get_by_role("button", name="Save text", exact=True).click()
                expect(page.locator(".original-text")).to_have_text(
                    f"Synthetic original {engine}"
                )
                expect(page.locator(".evidence-info-panel")).to_be_hidden()
                expect(page.get_by_text("Added by", exact=True)).to_be_hidden()
                expect(
                    page.get_by_role("heading", name="Findings", exact=True)
                ).to_be_visible()
                expect(page.get_by_role("link", name="Jump to findings")).to_have_count(
                    0
                )
                expect(page.get_by_text("Checking…", exact=True)).to_be_visible()
                self.open_evidence_panel(page, "info")
                expect(page.get_by_text("Record history", exact=True)).to_be_visible()
                expect(page.get_by_text("Added by", exact=True)).to_be_visible()
                expect(
                    page.get_by_text("Processing history", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role(
                        "link",
                        name=re.compile(r"Extract identifiers and dates.*Queued"),
                    )
                ).to_be_visible()
                self.capture(page, engine, "automatic-analysis-1440x900")
                page.set_viewport_size({"width": 390, "height": 844})
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 391
                )
                self.capture(page, engine, "automatic-analysis-390x844")
                page.set_viewport_size({"width": 1440, "height": 900})
                expect(
                    page.get_by_text("Synthetic source context", exact=True)
                ).to_be_visible()
                self.open_evidence_panel(page, "more")
                page.get_by_role("link", name="Edit details", exact=True).click()
                expect(extra).to_have_attribute("open", "")
                expect(page.get_by_label("Source URL", exact=True)).to_have_value(
                    "https://example.com/source"
                )

    def test_communication_evidence_and_retained_attachments(self):
        sender = Person.objects.create(case=self.case, first_name="Alex Sender")
        recipient = Person.objects.create(case=self.case, first_name="Jordan Recipient")
        viewer = self.make_user("communications-viewer")
        audio_filename = "synthetic-" + "recording" * 16 + ".wav"
        CaseMembership.objects.create(case=self.case, user=viewer)
        for engine in self.engines:
            for kind in ("text-message", "phone-call", "facetime"):
                with (
                    self.subTest(engine=engine, kind=kind),
                    self.browser_page(engine, java_script_enabled=False) as (_, page),
                ):
                    self.sign_in(page)
                    page.goto(
                        self.url("core:evidence:create", case_id=self.case.pk)
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
                                "No transcript or audio.",
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
                                "name": audio_filename,
                                "mimeType": "audio/wav",
                                "buffer": b"Synthetic audio\x00\r\n",
                            }
                        )
                        self.capture(page, engine, f"{kind}-attachment-mobile")
                        page.get_by_role(
                            "button", name="Add transcript or audio", exact=True
                        ).click()
                        filename_heading = page.get_by_role(
                            "heading", name=audio_filename, exact=True
                        )
                        expect(filename_heading).to_be_visible()
                        page.set_viewport_size({"width": 320, "height": 844})
                        filename_heading.scroll_into_view_if_needed()
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"), 321
                        )
                        self.capture(page, engine, f"{kind}-long-filename-320")
                        with page.expect_download() as event:
                            page.get_by_role(
                                "link", name="Download audio", exact=True
                            ).click()
                        self.assertEqual(
                            Path(event.value.path()).read_bytes(),
                            b"Synthetic audio\x00\r\n",
                        )
                        self.assertEqual(event.value.suggested_filename, audio_filename)
                        with page.expect_download() as transcript:
                            page.get_by_role(
                                "link", name="Download transcript", exact=True
                            ).click()
                        self.assertEqual(
                            Path(transcript.value.path()).read_bytes(),
                            # Native forms submit textarea line endings as CRLF.
                            b"  Supplied synthetic transcript <source>\r\n  ",
                        )
                        self.open_evidence_panel(page, "info")
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
                        page.locator(".evidence-info > summary").press("Enter")
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
                            page.get_by_role("heading", name=audio_filename, exact=True)
                        ).to_be_visible()
                    # Capture before attachment navigation, which can leave Linux
                    # WebKit waiting for font readiness after a download.
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
                    with page.expect_download() as event:
                        page.get_by_role(
                            "link", name="Download original", exact=True
                        ).click()
                    data = json.loads(Path(event.value.path()).read_bytes())
                    self.assertEqual(data["sender_id"], str(sender.pk))
                    self.assertEqual(data["recipient_id"], str(recipient.pk))
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
                page.goto(self.url("vehicle:list", case_id=self.case.pk))
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
                expect(page.get_by_label("Export timezone", exact=True)).to_have_value(
                    "America/Toronto"
                )
                page.get_by_label("Vehicle", exact=True).select_option(str(vehicle.pk))
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_label("Vehicle", exact=True).evaluate(
                    "element => element.scrollIntoView({block: 'center'})"
                )
                self.capture(page, engine, "vehicle-import-upload-controls-390")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.get_by_label("Source name or citation").fill(
                    "Synthetic tracking export"
                )
                page.get_by_label(
                    "Use Created Time as the reported observation time"
                ).check()
                page.get_by_role("button", name="Upload CSV").click()
                expect(
                    page.get_by_role("heading", name="Import vehicle locations")
                ).to_be_visible()
                expect(
                    page.get_by_role("heading", name="Preparing locations")
                ).to_be_visible()
                self.capture(page, engine, "vehicle-import-preparing")
                for width in (390, 320):
                    page.set_viewport_size({"width": width, "height": 844})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"vehicle-import-preparing-{width}")
                    page.locator("#processing-heading").evaluate(
                        "element => element.scrollIntoView({block: 'start'})"
                    )
                    self.capture(
                        page, engine, f"vehicle-import-preparing-progress-{width}"
                    )
                    page.evaluate("window.scrollTo(0, 0)")
                page.set_viewport_size({"width": 1440, "height": 900})
                import_id = page.url.rstrip("/").rsplit("/", 1)[-1]

                def process_current_import(import_id=import_id):
                    batch = VehicleLocationImport.objects.get(pk=import_id)
                    return process_import(str(batch.pk), str(batch.job_token))

                self.db(process_current_import)
                refresh = page.get_by_role("link", name="Refresh status")
                refresh.focus()
                refresh.press("Enter")
                expect(page.locator("select[name=vehicle_0]")).to_have_value(
                    str(vehicle.pk)
                )
                preview = page.locator("summary").filter(
                    has_text=re.compile(r"^1 source row · Preview$")
                )
                expect(page.locator(".import-observation summary")).to_be_hidden()
                preview.press("Enter")
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
                page.get_by_role("button", name="Import locations", exact=True).focus()
                page.get_by_role("button", name="Import locations", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role("heading", name="Importing observations")
                ).to_be_visible()
                self.capture(page, engine, "vehicle-import-publishing")
                page.set_viewport_size({"width": 390, "height": 844})
                self.capture(page, engine, "vehicle-import-publishing-390")
                page.set_viewport_size({"width": 1440, "height": 900})
                self.db(process_current_import)
                page.get_by_role("link", name="Refresh status").click()
                expect(
                    page.get_by_role("heading", name="Vehicle locations imported")
                ).to_be_visible()
                self.capture(page, engine, "vehicle-import-complete")
                expect(page.get_by_role("link", name="View findings")).to_be_visible()
                page.get_by_text("Imported observations", exact=True).press("Enter")
                page.locator(".import-results a").click()
                expect(
                    page.get_by_role("heading", name="Reported location")
                ).to_be_visible()
                expect(page.get_by_text("0.000 km/h", exact=True)).to_be_visible()
                receipt = page.get_by_text("Source receipt time", exact=True)
                expect(receipt).to_be_hidden()
                self.open_evidence_panel(page, "info")
                expect(
                    page.get_by_text("Original CSV row and interpretation", exact=True)
                ).to_be_visible()
                expect(receipt).to_be_visible()
                page.locator(".evidence-info > summary").press("Enter")
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
                    self.capture(
                        page, engine, f"vehicle-location-original-{width}x{height}"
                    )
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "vehicle-location-original-dark")
                page.emulate_media(color_scheme="light")
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
                    info = self.open_evidence_panel(viewer_page, "info")
                    expect(info.get_by_text("Your access", exact=True)).to_be_visible()
                    expect(info.get_by_text("Viewer", exact=True)).to_be_visible()
                    viewer_page.locator(".evidence-info > summary").press("Enter")
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

    def test_vehicle_location_duplicates_can_be_skipped_or_kept(self):
        vehicle = Vehicle.objects.create(
            case=self.case, model="Synthetic duplicate van"
        )

        def prepare_duplicate_import(engine, keep):
            row = parse_csv(
                csv_content(**{"Asset": f"Synthetic {engine} {keep}"}), "UTC"
            )[0].raw
            original = csv_content(row, row)
            document = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title="Synthetic duplicate CSV",
                file=SimpleUploadedFile("synthetic-duplicates.csv", original),
            )
            batch = VehicleLocationImport.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                document=document,
                timezone="UTC",
                selected_vehicle=vehicle,
            )
            enqueue_preparation(self.owner, self.case.pk, batch.pk)
            batch.refresh_from_db()
            process_import(str(batch.pk), str(batch.job_token))
            return batch.pk

        for engine in self.engines:
            for keep in (False, True):
                batch_id = self.db(prepare_duplicate_import, engine, keep)
                with (
                    self.subTest(engine=engine, keep=keep),
                    self.browser_page(engine, java_script_enabled=keep) as (_, page),
                ):
                    self.sign_in(page)
                    page.goto(
                        self.url(
                            "geo:vehicle_import_review",
                            case_id=self.case.pk,
                            import_id=batch_id,
                        )
                    )
                    choice = page.get_by_label(
                        "Keep the possible duplicates as separate observations"
                    )
                    expect(choice).not_to_be_checked()
                    self.assertFalse(choice.get_attribute("required"))
                    choice.focus()
                    expect(choice).to_be_focused()
                    self.assertNotEqual(
                        choice.evaluate("node => getComputedStyle(node).outlineStyle"),
                        "none",
                    )
                    if keep:
                        choice.press("Space")
                        expect(choice).to_be_checked()
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
                        choice.evaluate(
                            "node => node.scrollIntoView({block: 'center'})"
                        )
                        self.capture(page, engine, f"vehicle-duplicates-{keep}-{width}")
                    page.set_viewport_size({"width": 1440, "height": 900})
                    for appearance in ("dark", "light"):
                        page.emulate_media(
                            color_scheme=appearance, reduced_motion="reduce"
                        )
                        self.capture(
                            page, engine, f"vehicle-duplicates-{keep}-{appearance}"
                        )
                    page.emulate_media(forced_colors="active")
                    self.capture(
                        page, engine, f"vehicle-duplicates-{keep}-forced-colors"
                    )
                    page.emulate_media(forced_colors="none")
                    confirmation = page.get_by_label(
                        "Import these observations with the selected "
                        "vehicle associations"
                    )
                    confirmation.focus()
                    confirmation.press("Space")
                    page.get_by_role(
                        "button", name="Import locations", exact=True
                    ).focus()
                    page.get_by_role(
                        "button", name="Import locations", exact=True
                    ).press("Enter")
                    expect(
                        page.get_by_role("heading", name="Importing observations")
                    ).to_be_visible()

                    def publish(import_id=batch_id):
                        current = VehicleLocationImport.objects.get(pk=import_id)
                        return process_import(str(current.pk), str(current.job_token))

                    self.assertEqual(self.db(publish), "complete")
                    page.get_by_role("link", name="Refresh status").click()
                    expected = 2 if keep else 1
                    expect(page.locator(".import-results a")).to_have_count(expected)
                    expect(
                        page.get_by_text(f"{expected} observation", exact=False)
                    ).to_be_visible()
                    if not keep:
                        expect(
                            page.get_by_text(
                                "1 possible duplicate row skipped.", exact=False
                            )
                        ).to_be_visible()
                    self.capture(page, engine, f"vehicle-duplicates-{keep}-complete")
                    page.set_viewport_size({"width": 390, "height": 844})
                    self.capture(
                        page, engine, f"vehicle-duplicates-{keep}-complete-390"
                    )

    def evidence_url(self, record=None, *, case=None):
        return self.url(
            "core:evidence:detail",
            case_id=(case or self.case).pk,
            kind="text",
            evidence_id=(record or self.records[-1]).pk,
        )

    def test_vehicle_workflow_responsive_keyboard_and_read_only_access(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.url("cases:case_detail", case_id=self.case.pk))
                expect(page).to_have_url(
                    self.url("core:evidence:list", case_id=self.case.pk)
                )
                case_tools = page.locator(".case-tools")
                vehicles_link = case_tools.get_by_role(
                    "link", name="Vehicles", exact=True
                )
                expect(vehicles_link).to_be_hidden()
                case_tools.locator(":scope > summary").click()
                vehicles_link.click()
                expect(case_tools).not_to_have_attribute("open", "")
                expect(case_tools.locator(":scope > summary")).to_have_class(
                    re.compile("is-current")
                )
                case_tools.locator(":scope > summary").click()
                expect(
                    page.get_by_role("navigation", name="Investigation").locator(
                        '[aria-current="page"]'
                    )
                ).to_have_text("Vehicles")
                if engine == self.engines[0]:
                    expect(
                        page.get_by_role(
                            "heading", name="Keep the vehicles in this case together"
                        )
                    ).to_be_visible()
                page.get_by_role("link", name="Add vehicle").first.focus()
                page.get_by_role("link", name="Add vehicle").first.press("Enter")
                expect(page.get_by_label("Notes", exact=True)).to_have_count(0)
                page.get_by_label("Color", exact=True).fill("Slate blue")
                page.get_by_role("button", name="Add vehicle", exact=True).click()
                expect(page.locator(".error-summary")).to_be_focused()
                expect(page.get_by_label("Color", exact=True)).to_have_value(
                    "Slate blue"
                )
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
                    expect(
                        page.get_by_role("link", name=title, exact=True).locator(
                            ".collection-row-meta"
                        )
                    ).to_contain_text(f"License plate: SYN-{engine}")
                    self.capture(page, engine, f"vehicle-list-{width}-{height}")
                page.get_by_role("link", name=title, exact=True).click()
                page.get_by_role("link", name="Edit vehicle").click()
                page.get_by_label("Color", exact=True).fill("Slate blue")
                self.db(
                    Vehicle.objects.filter(
                        case=self.case, license_plate=f"SYN-{engine}"
                    ).update,
                    color="A newer color",
                    updated_at=self.case.updated_at,
                )
                page.get_by_role("button", name="Save changes", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Review the latest saved details")
                ).to_be_visible()
                expect(page.get_by_label("Color", exact=True)).to_have_value(
                    "Slate blue"
                )
                page.get_by_role("button", name="Save reviewed changes").click()
                expect(page.get_by_text("Slate blue", exact=True)).to_be_visible()

            viewer = self.make_user(f"vehicle-viewer-{engine}")
            CaseMembership.objects.create(case=self.case, user=viewer)
            # Verify ordinary links/forms without JavaScript for a read-only role.
            with self.browser_page(engine, java_script_enabled=False) as (_, page):
                self.sign_in(page, viewer)
                page.goto(self.url("vehicle:list", case_id=self.case.pk))
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
                page.goto(self.url("vehicle:list", case_id=self.case.pk))
                page.get_by_role("link", name="Add vehicle").first.click()
                page.get_by_label("License plate", exact=True).fill(f"DELETE-{engine}")
                page.get_by_role("button", name="Add vehicle", exact=True).click()
                details = page.locator("details").filter(
                    has=page.locator("summary", has_text="Record details")
                )
                details.locator("summary").focus()
                details.locator("summary").press("Enter")
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
            Permission.objects.filter(content_type__app_label__in=("text", "document"))
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
                page.goto(self.url("admin:text_textevidence_add"))
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
                expect(page.get_by_label("Source:", exact=True)).to_be_focused()
                source_details = page.locator("summary").filter(
                    has_text="Additional source details"
                )
                expect(page.get_by_label("Source url:", exact=True)).to_be_hidden()
                source_details.focus()
                source_details.press("Enter")
                expect(page.get_by_label("Source url:", exact=True)).to_be_visible()
                source_details.press("Enter")
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
                        "admin:document_documentevidence_change", args=[document.pk]
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
                        self.url("core:evidence:create", case_id=self.case.pk)
                        + f"?kind={kind}"
                    )
                    page.locator("[data-additional-evidence-details] > summary").focus()
                    page.locator("[data-additional-evidence-details] > summary").press(
                        "Enter"
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
                    for width, height in ((1440, 900), (390, 844), (320, 844)):
                        page.set_viewport_size({"width": width, "height": height})
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"),
                            width + 1,
                        )
                        self.capture(page, engine, f"{kind}-original-{width}x{height}")
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
                    self.open_evidence_panel(page, "more")
                    page.get_by_role("link", name="Edit details", exact=True).click()
                    expect(page.locator("#id_address")).to_have_count(0)
                    page.get_by_label("Source", exact=True).fill(
                        "Updated synthetic source"
                    )
                    page.get_by_role("button", name="Save changes", exact=True).click()
                    self.db(record.refresh_from_db)
                    self.assertEqual(record.source, "Updated synthetic source")
                    self.assertEqual(hashlib.sha256(content).hexdigest(), record.sha256)

    def test_person_social_accounts_journey(self):
        person = Person.objects.create(case=self.case, first_name="Alex Browser")
        long_name_person = Person.objects.create(
            case=self.case, first_name="SyntheticName" * 10
        )
        detail_url = self.url(
            "people:detail", case_id=self.case.pk, person_id=person.pk
        )
        username = f"@Alex_Example@{'synthetic' * 10}.example.test"
        source = "Synthetic profile citation"

        def capture_layouts(page, engine, screen):
            for width, height in ((1440, 900), (320, 844)):
                page.set_viewport_size({"width": width, "height": height})
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"),
                    width + 1,
                    f"{engine}: {screen} overflow at {width}px",
                )
                if width == 320:
                    heading = (
                        page.get_by_role("heading", name="Social accounts", exact=True)
                        if screen == "detail"
                        else page.get_by_role("heading", level=1)
                    )
                    heading.evaluate("element => element.scrollIntoView()")
                self.capture(page, engine, f"person-social-{screen}-{width}x{height}")

        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(detail_url)
                accounts = page.get_by_role("region", name="Social accounts")
                add_link = accounts.get_by_role("link", name="Add social account")
                add_link.focus()
                expect(add_link).to_be_focused()
                add_link.press("Enter")

                platform_field = page.get_by_label("Platform", exact=True)
                platform_field.fill("Mastodon")
                platform_field.focus()
                platform_field.press("Tab")
                username_field = page.get_by_label("Username", exact=True)
                expect(username_field).to_be_focused()
                self.assertNotEqual(
                    username_field.evaluate(
                        "node => getComputedStyle(node).outlineStyle"
                    ),
                    "none",
                )
                username_field.fill(username)
                username_field.press("Tab")
                source_field = page.get_by_label("Source / citation", exact=True)
                expect(source_field).to_be_focused()
                source_field.fill(source)
                capture_layouts(page, engine, "add")
                page.get_by_role("button", name="Add social account").press("Enter")

                expect(page).to_have_url(detail_url + "#social-accounts")
                expect(accounts.get_by_text(username, exact=True)).to_be_visible()
                expect(accounts.get_by_text(source, exact=True)).to_be_visible()
                capture_layouts(page, engine, "detail")
                accounts.get_by_role(
                    "link", name=f"Edit social account for Mastodon {username}"
                ).press("Enter")
                expect(username_field).to_have_value(username)
                updated_source = "Updated synthetic profile citation"
                source_field.fill(updated_source)
                page.get_by_role("button", name="Save changes", exact=True).press(
                    "Enter"
                )
                expect(accounts.get_by_text(updated_source, exact=True)).to_be_visible()

                accounts.get_by_role(
                    "link", name=f"Edit social account for Mastodon {username}"
                ).press("Enter")
                page.get_by_role("link", name="Remove social account").press("Enter")
                expect(
                    page.get_by_role("heading", name="Remove social account?")
                ).to_be_visible()
                capture_layouts(page, engine, "remove")
                confirm = page.get_by_role("checkbox")
                confirm.focus()
                confirm.press("Space")
                expect(confirm).to_be_checked()
                page.get_by_role("button", name="Remove social account").press("Enter")
                expect(page).to_have_url(detail_url + "#social-accounts")
                expect(
                    accounts.get_by_text("No social accounts recorded.")
                ).to_be_visible()

                page.goto(
                    self.url(
                        "socialmedia:social_account_create",
                        case_id=self.case.pk,
                        person_id=long_name_person.pk,
                    )
                )
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 321
                )

    def test_management_and_collaboration_journey(self):
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.get_by_role("link", name="Create case", exact=True).first.click()
                name = f"Synthetic browser case {engine}"
                page.get_by_label("Case name", exact=True).fill(name)
                expect(page.get_by_label("Description", exact=True)).to_have_count(0)
                page.get_by_role("button", name="Create case", exact=True).click()
                case = self.db(Case.objects.get, name=name, owner=self.owner)
                expect(page).to_have_url(
                    self.url("core:evidence:list", case_id=case.pk)
                )
                expect(
                    page.get_by_role("heading", name="Evidence", exact=True)
                ).to_be_visible()
                expect(
                    page.locator(".evidence-case-switch").get_by_text(name)
                ).to_be_visible()

                page.goto(self.url("people:create", case_id=case.pk))
                page.get_by_label("First name", exact=True).fill("Alex")
                page.get_by_label("Last name", exact=True).fill("Browser Example")
                expect(page.get_by_label("Notes", exact=True)).to_have_count(0)
                page.get_by_role("button", name="Add person", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Alex Browser Example", exact=True)
                ).to_be_visible()
                self.db(
                    Person.objects.filter(case=case).update,
                    notes="Earlier synthetic context.",
                )
                page.get_by_role("link", name="Edit person", exact=True).click()
                expect(page.get_by_label("Notes", exact=True)).to_have_count(0)
                page.get_by_label("Email", exact=True).fill("alex@example.test")
                page.get_by_role("button", name="Save changes", exact=True).click()
                expect(page.get_by_text("Earlier synthetic context.")).to_be_hidden()
                page.locator("details > summary").filter(
                    has_text="Record details"
                ).click()
                expect(page.get_by_text("Earlier synthetic context.")).to_be_visible()
                self.assertEqual(
                    self.db(Person.objects.get, case=case).email,
                    "alex@example.test",
                )

                page.goto(
                    self.url("core:evidence:create", case_id=case.pk) + "?kind=text"
                )
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
                self.open_evidence_panel(page, "more")
                page.get_by_role("button", name="Archive", exact=True).click()
                self.open_evidence_panel(page, "more")
                expect(
                    page.get_by_role("button", name="Restore", exact=True)
                ).to_be_visible()
                page.get_by_role("button", name="Restore", exact=True).click()
                self.open_evidence_panel(page, "more")
                expect(
                    page.get_by_role("button", name="Archive", exact=True)
                ).to_be_visible()
                page.locator(".evidence-more > summary").press("Enter")
                page.locator(".original-text").scroll_into_view_if_needed()
                expect(page.locator(".original-text")).to_have_text(original)
                self.db(record.refresh_from_db)
                self.assertFalse(record.is_archived)
                self.assertEqual(record.sha256, checksum)

                page.goto(self.url("core:evidence:list", case_id=case.pk))
                page.get_by_label("Search evidence", exact=True).fill("captured source")
                page.get_by_label("Search evidence", exact=True).press("Enter")
                expect(page.locator(".evidence-row")).to_have_count(1)
                expect(page.locator(".evidence-row")).to_contain_text(title)

                page.goto(
                    self.url("core:evidence:create", case_id=case.pk) + "?kind=document"
                )
                files = [
                    {
                        "name": f"synthetic-{index}.txt",
                        "mimeType": "text/plain",
                        "buffer": f"Synthetic batch original {index}.".encode(),
                    }
                    for index in (1, 2)
                ]
                picker = page.locator("#id_file")
                entries = page.locator(".upload-entry")
                for width, height in ((390, 844), (1440, 900)):
                    page.set_viewport_size({"width": width, "height": height})
                    picker.set_input_files(files[0])
                    expect(picker).to_have_value("")
                    expect(entries).to_have_count(1)
                    entries.get_by_label("Title", exact=True).fill("Discarded draft")
                    entries.get_by_role("button", name="Remove from queue").press(
                        "Enter"
                    )
                    expect(entries).to_have_count(0)
                    expect(picker).to_be_focused()
                    expect(page.locator("#id_title")).to_be_visible()
                    expect(
                        page.locator("[data-additional-evidence-details]")
                    ).to_be_visible()
                    self.assertTrue(
                        page.evaluate(
                            "window.dispatchEvent("
                            "new Event('beforeunload', {cancelable: true}))"
                        )
                    )
                    self.capture(page, engine, f"single-upload-reset-{width}")
                picker.set_input_files(files)
                expect(entries).to_have_count(2)
                first_entry = entries.first
                first_entry.get_by_text("Source details", exact=True).click()
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
                    expect(guest_page).to_have_url(
                        self.url("core:evidence:list", case_id=case.pk)
                    )
                    expect(
                        guest_page.get_by_role("heading", name="Evidence", exact=True)
                    ).to_be_visible()
                    expect(
                        guest_page.locator(".evidence-case-switch").get_by_text(name)
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
                page.goto(self.url("core:evidence:create", case_id=self.case.pk))
                expect(page.locator("[data-bulk-fallback]")).to_be_visible()
                expect(page.locator("[data-bulk-upload]")).to_be_hidden()
                page.get_by_role("link", name="add one file at a time").click()
                expect(page).to_have_url(
                    self.url("core:evidence:create", case_id=self.case.pk)
                    + "?kind=document"
                )
                page.get_by_label("Title", exact=True).fill("Keep this proposed title")
                page.get_by_role("button", name="Upload file", exact=True).click()
                expect(page.locator("#id_title")).to_have_value(
                    "Keep this proposed title"
                )
                expect(page.get_by_role("heading", name="Add document")).to_be_visible()
                content = (
                    f"Synthetic source file {engine}.\r\n"
                    "Preserve these original bytes.\n"
                ).encode()
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
                self.capture(page, engine, "no-javascript-document")
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

                page.goto(
                    self.url("core:evidence:create", case_id=self.case.pk)
                    + "?kind=document"
                )
                page.get_by_label("Title", exact=True).fill(f"Separate copy {engine}")
                duplicate_file = {
                    "name": "renamed.txt",
                    "mimeType": "text/plain",
                    "buffer": content,
                }
                page.locator("#id_file").set_input_files(duplicate_file)
                page.get_by_role("button", name="Upload file", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Matching evidence already exists")
                ).to_be_visible()
                expect(page.get_by_label("Title", exact=True)).to_have_value(
                    f"Separate copy {engine}"
                )
                self.capture(page, engine, "duplicate-without-javascript")
                page.locator("#id_file").set_input_files(duplicate_file)
                confirm = page.get_by_role("button", name="Add duplicate anyway")
                confirm.focus()
                confirm.press("Enter")
                expect(
                    page.get_by_role(
                        "heading", name=f"Separate copy {engine}", exact=True
                    )
                ).to_be_visible()

                record = self.records[-1]
                page.goto(self.evidence_url(record))
                with page.expect_download() as text_original:
                    page.get_by_role(
                        "link", name="Download original", exact=True
                    ).click()
                self.assertEqual(
                    text_original.value.suggested_filename, f"evidence-{record.pk}.txt"
                )
                self.assertEqual(
                    Path(text_original.value.path()).read_bytes(),
                    record.body.encode("utf-8"),
                )

    def test_duplicate_upload_review_and_keyboard_confirmation(self):
        for engine in self.engines:
            original = f"Exact synthetic original {engine}".encode()
            retained = ImageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title="Earlier <untrusted> source " + "with a long title " * 8,
                file=SimpleUploadedFile("source.png", original),
            )
            retained.is_archived = True
            retained.archive_changed_at = retained.created_at
            retained.archive_changed_by = self.owner
            retained.save()
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(self.url("core:evidence:bulk_create", case_id=self.case.pk))
                page.locator("#id_file").set_input_files(
                    [
                        {
                            "name": "renamed.txt",
                            "mimeType": "text/plain",
                            "buffer": original,
                        },
                        {
                            "name": "different.txt",
                            "mimeType": "text/plain",
                            "buffer": original + b" changed",
                        },
                    ]
                )
                page.get_by_role("button", name="Upload files", exact=True).click()
                duplicate = page.locator('.upload-entry[data-state="duplicate"]')
                expect(duplicate).to_have_count(1)
                expect(page.locator('.upload-entry[data-state="added"]')).to_have_count(
                    1
                )
                expect(duplicate.locator(".notice")).to_contain_text("Archived")
                expect(duplicate.locator(".notice a")).to_have_count(1)
                self.assertEqual(
                    self.db(
                        DocumentEvidence.objects.filter(
                            case=self.case, sha256=retained.sha256
                        ).count
                    ),
                    0,
                )
                with page.expect_popup() as opened:
                    duplicate.locator(".notice a").click()
                match_page = opened.value
                expect(
                    match_page.get_by_role("heading", name=retained.title, exact=True)
                ).to_be_visible()
                match_page.close()
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
                    page.screenshot(
                        path=str(
                            self.artifacts / f"{engine}-duplicate-review-{width}.png"
                        ),
                        full_page=True,
                    )
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "duplicate-review-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "duplicate-review-forced-colors")
                    page.emulate_media(forced_colors="none")
                page.emulate_media(color_scheme="light")
                duplicate.get_by_label("Title", exact=True).fill(
                    f"Separate source {engine}"
                )
                confirm = duplicate.get_by_role("button", name="Add duplicate anyway")
                confirm.focus()
                confirm.press(
                    "Alt+Tab"
                    if engine == "webkit" and sys.platform == "darwin"
                    else "Tab"
                )
                page.keyboard.press(
                    "Alt+Shift+Tab"
                    if engine == "webkit" and sys.platform == "darwin"
                    else "Shift+Tab"
                )
                expect(confirm).to_be_focused()
                self.assertNotEqual(
                    confirm.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                confirm.press("Enter")
                expect(page.locator('.upload-entry[data-state="added"]')).to_have_count(
                    2
                )
                saved = self.db(
                    DocumentEvidence.objects.get,
                    case=self.case,
                    title=f"Separate source {engine}",
                )
                self.assertEqual(saved.sha256, retained.sha256)
                self.assertEqual(Path(saved.file.path).read_bytes(), original)

    def test_uncertain_commit_retry_and_proxy_size_limit_recovery(self):
        for engine in self.engines:
            original = (
                f"Synthetic original with interrupted confirmation {engine}.".encode()
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                upload_url = self.url("core:evidence:create", case_id=self.case.pk)
                bulk_url = self.url("core:evidence:bulk_create", case_id=self.case.pk)
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

                def commit_then_abort(route, *, original=original):
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
                                    original in (route.request.post_data_buffer or b"")
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
                        "buffer": (
                            f"Synthetic original rejected before Django {engine}."
                        ).encode(),
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
                    {
                        **item,
                        "name": f"{engine}-{item['name']}",
                        "buffer": engine.encode() + item["buffer"],
                    }
                    for item in originals
                ]
                page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                page.locator(".evidence-list-heading").get_by_role(
                    "link", name="Add evidence", exact=True
                ).click()
                expect(page).to_have_url(
                    self.url("core:evidence:create", case_id=self.case.pk)
                )
                expect(
                    page.get_by_role("heading", name="Add evidence", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name="Add text", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role(
                        "link", name="Import vehicle locations", exact=True
                    )
                ).to_be_visible()
                expect(page.locator("[data-bulk-fallback]")).to_be_hidden()
                expect(page.locator("#upload-queue")).to_be_hidden()
                expect(page.locator("[data-upload-actions]")).to_be_hidden()
                expect(page.locator("[data-stop-uploads]")).to_be_hidden()
                picker = page.locator("#id_file")
                picker.set_input_files(files + files[:2])
                expect(page.locator(".upload-entry")).to_have_count(0)
                expect(page.locator("[data-upload-status]")).to_be_visible()
                expect(page.locator("[data-upload-status]")).to_contain_text(
                    "room for 4 more files"
                )
                expect(page.locator("[data-upload-actions]")).to_be_hidden()
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
                picker.set_input_files(files[:2])
                entries = page.locator(".upload-entry")
                expect(entries).to_have_count(2)
                expect(page.locator("#upload-queue")).to_be_visible()
                expect(page.locator("[data-upload-actions]")).to_be_visible()
                expect(page.locator("[data-stop-uploads]")).to_be_hidden()
                first, second = entries.nth(0), entries.nth(1)
                first.get_by_text("Source details", exact=True).click()
                first.get_by_label("Source", exact=True).fill("Individual witness")
                second.get_by_text("Source details", exact=True).click()
                second.get_by_label("Source", exact=True).fill("")
                second.get_by_label("Source URL", exact=True).fill("invalid-url")
                picker.set_input_files(files[2])
                expect(entries).to_have_count(3)
                third = entries.nth(2)
                third.get_by_text("Source details", exact=True).click()
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
                upload_url = self.url("core:evidence:create", case_id=self.case.pk)

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
                second.get_by_text("Source details", exact=True).click()
                second.get_by_label("Source URL", exact=True).fill(
                    "https://example.com/corrected"
                )
                second.get_by_role("button", name="Retry this file").click()
                expect(second).to_have_attribute("data-state", "added")
                expect(
                    page.get_by_role("link", name="Start another batch")
                ).to_be_visible()
                # Another selection of the same original needs explicit review.
                picker.set_input_files(files[0])
                expect(entries).to_have_count(4)
                expect(
                    page.get_by_role("link", name="Start another batch")
                ).to_be_hidden()
                page.get_by_role("button", name="Upload files", exact=True).click()
                expect(entries.nth(3)).to_have_attribute("data-state", "duplicate")
                entries.nth(3).get_by_role(
                    "button", name="Add duplicate anyway"
                ).click()
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
                expect(page.locator("#upload-queue")).to_be_hidden()
                expect(page.locator("[data-upload-actions]")).to_be_hidden()

    @override_settings(PANDECT_MAX_FILE_BYTES=16)
    def test_bulk_invalid_files_removal_and_access_removed_between_uploads(self):
        for engine in self.engines:
            editor = self.make_user(f"bulk-editor-{engine}")
            membership = CaseMembership.objects.create(
                case=self.case, user=editor, role="editor"
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, editor)
                page.goto(self.url("core:evidence:bulk_create", case_id=self.case.pk))
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
                expect(page.locator("#upload-queue")).to_be_hidden()
                expect(page.locator("[data-upload-actions]")).to_be_hidden()
                expect(picker).to_be_focused()
                picker.set_input_files(
                    [
                        {
                            "name": f"{engine}-{i}.txt",
                            "mimeType": "text/plain",
                            "buffer": f"{engine}-{i}".encode(),
                        }
                        for i in range(3)
                    ]
                )

                def pause_first(route):
                    page.get_by_role("button", name="Stop after this file").click()
                    route.continue_()

                page.route(
                    self.url("core:evidence:create", case_id=self.case.pk),
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
                navigation = page.get_by_role("navigation", name="Investigation")
                expect(navigation).to_have_count(1)
                for width in (390, 1440, 768, 2616):
                    page.set_viewport_size({"width": width, "height": 900})
                    expect(
                        navigation.get_by_role("link", name="Evidence", exact=True)
                    ).to_be_visible()
                    expect(navigation.locator(".case-tools > summary")).to_be_visible()
                    if width < 1024:
                        self.assertLessEqual(
                            page.locator(".app-navigation").bounding_box()["height"], 80
                        )
                self.capture(page, engine, "workspace-ultrawide")
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

                self.open_evidence_panel(page, "more")
                edit = page.get_by_role("link", name="Edit details", exact=True)
                edit.focus()
                expect(edit).to_be_focused()
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
                    expect(edit).to_be_visible()
                    self.capture(page, engine, f"evidence-details-{width}x{height}")

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

    def test_long_original_keeps_findings_and_info_reachable(self):
        original = "\n\n".join(
            f"Synthetic source paragraph {number}. "
            "Retain the complete original while keeping the findings and "
            "source details reachable with a keyboard."
            for number in range(1, 81)
        )
        record = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Long synthetic retained source",
            body=original,
            source="Synthetic long-source fixture",
        )
        url = self.url(
            "core:evidence:detail",
            case_id=self.case.pk,
            kind="text",
            evidence_id=record.pk,
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(url)
                expect(page.locator(".original-text")).to_have_text(original)
                expect(page.locator("h1")).to_have_count(1)
                expect(page.get_by_role("link", name="Jump to findings")).to_have_count(
                    0
                )
                expect(page.locator(".evidence-info-panel")).to_be_hidden()
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(url)
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"quiet-long-original-{width}x{height}")
                    page.locator(
                        "#evidence-findings-heading"
                    ).scroll_into_view_if_needed()
                    self.capture(page, engine, f"quiet-long-findings-{width}x{height}")
                    self.open_evidence_panel(page, "info")
                    expect(page.get_by_text("Added by", exact=True)).to_be_visible()
                    expect(
                        page.get_by_text("Record history", exact=True)
                    ).to_be_visible()
                    expect(page.locator(".original-text")).to_have_text(original)
                    self.capture(page, engine, f"quiet-long-info-{width}x{height}")
                    page.locator(".evidence-info > summary").press("Enter")
                page.locator('input[name="submission_id"]').evaluate(
                    "input => { input.value = 'invalid'; }"
                )
                page.get_by_role("button", name="Run checks", exact=True).press("Enter")
                error = page.locator("#evidence-analysis-error")
                expect(error).to_contain_text("Checks could not start")
                expect(error).to_be_in_viewport()
                expect(error).to_be_focused()
                expect(page.locator(".original-text")).to_have_text(original)
                expect(page.locator(".evidence-info-panel")).to_be_hidden()
                self.capture(page, engine, "quiet-long-original-check-error")

    def test_quiet_evidence_workspace_panels_and_visual_target(self):
        from datetime import UTC, datetime

        from core.analysis.services import process_run, start_run
        from core.analysis.testing import image_bytes
        from phone.models import PhoneCallEvidence

        case = Case.objects.create(owner=self.owner, name="Harbour records")
        caller = Person.objects.create(case=case, first_name="Synthetic witness")
        recipient = Person.objects.create(
            case=case, first_name="Synthetic receptionist"
        )
        title = "Interview — Harbour office"
        original = (
            "The harbour office was closed when I arrived at 16:30 on 12 November. "
            "A blue van was parked by the east gate. I called the number on the door, "
            "416-555-0142, but no one answered. I returned the following morning "
            "and spoke with the receptionist."
        )
        titles = (
            "East gate photograph",
            "Vehicle locations.csv",
            "Gatehouse call",
            title,
            "Reception follow-up",
            "Harbour address",
            "Office exterior",
            "Call transcript",
            "Site notes",
            "Delivery receipt",
            "West entrance",
            "Morning observations",
            "North gate notes",
            "Loading bay observations",
        )
        for source_title in reversed(titles):
            values = {
                "case": case,
                "uploaded_by": self.owner,
                "title": source_title,
                "collected_at": datetime(2026, 11, 12, 16, 30, tzinfo=UTC),
            }
            if source_title in {
                "East gate photograph",
                "Office exterior",
                "West entrance",
            }:
                ImageEvidence.objects.create(
                    **values,
                    file=SimpleUploadedFile("gate.png", image_bytes(), "image/png"),
                )
            elif source_title == "Vehicle locations.csv":
                DocumentEvidence.objects.create(
                    **values,
                    file=SimpleUploadedFile(
                        "locations.csv", b"address,time\nHarbour,16:30\n", "text/csv"
                    ),
                )
            elif source_title == "Gatehouse call":
                PhoneCallEvidence.objects.create(
                    **values, sender=caller, recipient=recipient
                )
            elif source_title == "Harbour address":
                LocationEvidence.objects.create(**values, address="Harbour office")
            else:
                record = TextEvidence.objects.create(
                    **values,
                    body=original
                    if source_title == title
                    else "Synthetic retained source.",
                    source="Witness interview" if source_title == title else "",
                )
                if source_title == title:
                    selected = record
        run, _ = start_run(
            self.owner, case.pk, "entities", [{"kind": "text", "id": str(selected.pk)}]
        )
        process_run(run.pk)
        url = self.url(
            "core:evidence:detail",
            case_id=case.pk,
            kind="text",
            evidence_id=selected.pk,
        )
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(url)
                expect(page.locator(".original-text")).to_have_text(original)
                expect(
                    page.locator(".analysis-finding-rows > li").first
                ).to_be_visible()
                expect(page.locator(".evidence-info-panel")).to_be_hidden()
                expect(page.locator(".evidence-more-panel")).to_be_hidden()
                expect(page.get_by_role("link", name="Jump to findings")).to_have_count(
                    0
                )
                for width, height in (
                    (1440, 1024),
                    (1440, 900),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    page.locator("h1").scroll_into_view_if_needed()
                    self.capture(page, engine, f"quiet-evidence-{width}x{height}")
                    info = self.open_evidence_panel(page, "info")
                    expect(info.locator(".evidence-info-panel")).to_be_visible()
                    expect(
                        info.get_by_text("Processing history", exact=True)
                    ).to_be_visible()
                    expect(info.get_by_text("Added by", exact=True)).to_be_visible()
                    self.capture(page, engine, f"quiet-evidence-info-{width}x{height}")
                    page.keyboard.press("Escape")
                    expect(info).not_to_have_attribute("open", "")
                    expect(info.locator(":scope > summary")).to_be_focused()
                    more = self.open_evidence_panel(page, "more")
                    expect(
                        more.get_by_role("link", name="Edit details")
                    ).to_be_visible()
                    expect(
                        more.get_by_role("button", name="Archive", exact=True)
                    ).to_be_visible()
                    self.capture(page, engine, f"quiet-evidence-more-{width}x{height}")
                    page.keyboard.press("Escape")
                    expect(more).not_to_have_attribute("open", "")
                    expect(more.locator(":scope > summary")).to_be_focused()
                    for summary in (
                        info.locator(":scope > summary"),
                        more.locator(":scope > summary"),
                    ):
                        self.assertGreaterEqual(summary.bounding_box()["height"], 44)
                page.set_viewport_size({"width": 1440, "height": 1024})
                info = self.open_evidence_panel(page, "info")
                page.locator("h1").click()
                expect(info).not_to_have_attribute("open", "")
                more = self.open_evidence_panel(page, "more")
                page.locator("h1").click()
                expect(more).not_to_have_attribute("open", "")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "quiet-evidence-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "quiet-evidence-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                page.locator(".analysis-finding-link").first.press("Enter")
                expect(page).not_to_have_url(url)
                expect(page.locator(".analysis-finding").first).to_be_visible()

    def test_superuser_evidence_deletion(self):
        self.owner.is_staff = self.owner.is_superuser = True
        self.owner.save(update_fields=["is_staff", "is_superuser"])
        for engine in self.engines:
            document = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Synthetic original {engine}",
                file=SimpleUploadedFile("original.txt", b"Synthetic original"),
            )
            change_url = self.live_server_url + reverse(
                "admin:document_documentevidence_change", args=[document.pk]
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(change_url)
                page.get_by_role("button", name="Delete", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Delete", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("This permanently deletes", exact=False)
                ).to_be_visible()
                confirm = page.get_by_role("button", name="Permanently delete evidence")
                confirm.focus()
                expect(confirm).to_be_focused()
                confirm.press(
                    "Alt+Tab"
                    if engine == "webkit" and sys.platform == "darwin"
                    else "Tab"
                )
                expect(page.get_by_role("button", name="Cancel")).to_be_focused()
                page.get_by_role("button", name="Cancel").press("Enter")
                expect(page).to_have_url(change_url)
                self.assertTrue(
                    self.db(DocumentEvidence.objects.filter(pk=document.pk).exists)
                )
                page.get_by_role("button", name="Delete", exact=True).click()
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    expect(confirm).to_be_visible()
                    self.assertLessEqual(
                        page.evaluate(
                            "document.documentElement.scrollWidth - innerWidth"
                        ),
                        1,
                    )
                    self.capture(page, engine, f"evidence-delete-{width}-{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "evidence-delete-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    confirm.focus()
                    expect(confirm).to_be_focused()
                    self.capture(page, engine, "evidence-delete-forced-colors")
            with self.browser_page(engine, java_script_enabled=False) as (_, page):
                self.sign_in(page)
                page.goto(change_url)
                page.get_by_role("button", name="Delete", exact=True).click()
                page.get_by_role("button", name="Cancel").click()
                expect(page).to_have_url(change_url)
                page.get_by_role("button", name="Delete", exact=True).click()
                page.get_by_role("button", name="Permanently delete evidence").press(
                    "Enter"
                )
                expect(
                    page.get_by_role(
                        "heading", name="Select document evidence to change"
                    )
                ).to_be_visible()
                self.assertFalse(
                    self.db(DocumentEvidence.objects.filter(pk=document.pk).exists)
                )
                self.assertFalse(document.file.storage.exists(document.file.name))

    def test_in_app_superuser_evidence_deletion(self):
        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_superuser"])
        for engine in self.engines:
            document = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Synthetic deletion {engine}",
                file=SimpleUploadedFile("original.txt", b"Synthetic original"),
            )
            source = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Retained import {engine}",
                file=SimpleUploadedFile("source.csv", b"Synthetic retained CSV"),
            )
            VehicleLocationImport.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                document=source,
                timezone="UTC",
            )
            detail_url = self.url(
                "core:evidence:detail",
                case_id=self.case.pk,
                kind="document",
                evidence_id=document.pk,
            )
            suffix = "?q=Synthetic&type=document&state=all&page=1"
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(detail_url + suffix)
                self.open_evidence_panel(page, "more")
                delete_link = page.get_by_role(
                    "link", name="Delete evidence", exact=True
                )
                delete_link.focus()
                expect(delete_link).to_be_focused()
                self.assertNotEqual(
                    delete_link.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                self.capture(page, engine, "product-evidence-delete-details-desktop")
                self.open_evidence_panel(page, "more")
                delete_link.click()
                expect(
                    page.get_by_role("heading", name=f"Delete {document.title}?")
                ).to_be_visible()
                self.assertTrue(
                    self.db(DocumentEvidence.objects.filter(pk=document.pk).exists)
                )
                check = page.get_by_role("checkbox")
                check.focus()
                check.press("Space")
                expect(check).to_be_checked()
                check.press(
                    "Alt+Tab"
                    if engine == "webkit" and sys.platform == "darwin"
                    else "Tab"
                )
                expect(
                    page.get_by_role("link", name="Cancel", exact=True)
                ).to_be_focused()
                page.get_by_role("link", name="Cancel", exact=True).press("Enter")
                expect(page).to_have_url(
                    detail_url + suffix + "&info=1#evidence-details-heading"
                )
                self.open_evidence_panel(page, "more")
                delete_link.click()
                confirm = page.get_by_role("button", name="Permanently delete evidence")
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    expect(confirm).to_be_visible()
                    self.assertLessEqual(
                        page.evaluate(
                            "document.documentElement.scrollWidth - innerWidth"
                        ),
                        1,
                    )
                    self.assertGreaterEqual(confirm.bounding_box()["height"], 44)
                    self.capture(
                        page, engine, f"product-evidence-delete-{width}-{height}"
                    )
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "product-evidence-delete-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    confirm.focus()
                    expect(confirm).to_be_focused()
                    self.capture(page, engine, "product-evidence-delete-forced-colors")
                    page.emulate_media(forced_colors="none")
                page.emulate_media(color_scheme="light")
                page.goto(
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="document",
                        evidence_id=source.pk,
                    )
                    + "?section=details"
                )
                self.open_evidence_panel(page, "more")
                page.get_by_role("link", name="Delete evidence", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Exactly what will be removed")
                ).to_be_visible()
                expect(confirm).to_be_visible()
                self.capture(page, engine, "product-evidence-delete-import-source")
                page.get_by_role("link", name="Cancel", exact=True).click()
                expect(
                    page.get_by_role("heading", name=source.title, exact=True)
                ).to_be_visible()
            with self.browser_page(engine, java_script_enabled=False) as (_, page):
                self.sign_in(page)
                page.goto(detail_url + suffix + "&info=1#evidence-details-heading")
                page.set_viewport_size({"width": 390, "height": 844})
                delete_link = page.get_by_role(
                    "link", name="Delete evidence", exact=True
                )
                self.open_evidence_panel(page, "more")
                delete_link.focus()
                self.capture(page, engine, "product-evidence-delete-details-mobile")
                delete_link.press("Enter")
                page.get_by_role("link", name="Cancel", exact=True).click()
                expect(page).to_have_url(
                    detail_url + suffix + "&info=1#evidence-details-heading"
                )
                self.open_evidence_panel(page, "more")
                delete_link.click()
                page.get_by_role("checkbox").check()
                page.get_by_role("button", name="Permanently delete evidence").press(
                    "Enter"
                )
                expect(page).to_have_url(
                    self.url("core:evidence:list", case_id=self.case.pk) + suffix
                )
                expect(
                    page.get_by_text("Evidence deleted.", exact=True)
                ).to_be_visible()
                self.assertFalse(
                    self.db(DocumentEvidence.objects.filter(pk=document.pk).exists)
                )
                self.assertFalse(document.file.storage.exists(document.file.name))

    def test_imported_source_deletion_inventory(self):
        from django.utils import timezone

        from core.models import AnalysisArtifact, AnalysisResult, AnalysisRun
        from geo.models import VehicleLocationImportRow

        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_superuser"])
        for engine in self.engines:
            vehicle = Vehicle.objects.create(
                case=self.case, model=f"Retained van {engine}"
            )
            row = parse_csv(csv_content(), "UTC")[0]
            source = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Imported source {engine}",
                file=SimpleUploadedFile(
                    "imported-source.csv", csv_content(*[row.raw for _ in range(55)])
                ),
            )
            batch = VehicleLocationImport.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                document=source,
                timezone="UTC",
                status="complete",
                completed_at=timezone.now(),
                row_count=55,
            )
            observations = []
            for index in range(55):
                observations.append(
                    VehicleLocationEvidence.objects.create(
                        case=self.case,
                        uploaded_by=self.owner,
                        vehicle=vehicle,
                        title=f"Imported position {index + 1}",
                        import_batch=batch,
                        source_row=index + 2,
                        raw_row=row.raw,
                        **row.values,
                    )
                )
            VehicleLocationImportRow.objects.bulk_create(
                [
                    VehicleLocationImportRow(
                        batch=batch,
                        number=index + 2,
                        group_index=0,
                        raw=row.raw,
                        fingerprint=row.fingerprint,
                    )
                    for index in range(55)
                ]
            )
            source_id = str(observations[0].pk)
            run = AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.owner,
                operation="location_quality",
                method_version="1",
                status="complete",
                inputs=[
                    {
                        "id": source_id,
                        "kind": "vehicle-location",
                        "evidence_id": source_id,
                    }
                ],
            )
            result = AnalysisResult.objects.create(case=self.case, run=run)
            artifact = AnalysisArtifact.objects.create(
                case=self.case,
                result=result,
                source_id=source_id,
                file=SimpleUploadedFile("preview.jpg", b"Synthetic derived image"),
                sha256="a" * 64,
                width=1,
                height=1,
            )
            url = self.url(
                "core:evidence:detail",
                case_id=self.case.pk,
                kind="document",
                evidence_id=source.pk,
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(url + "?section=details")
                self.open_evidence_panel(page, "more")
                page.get_by_role("link", name="Delete evidence", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Exactly what will be removed")
                ).to_be_visible()
                expect(
                    page.get_by_text("imported-source.csv", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("Generated image preview", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("115 records and 2 stored files.", exact=True)
                ).to_be_visible()
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
                    self.capture(
                        page, engine, f"source-deletion-inventory-{width}-{height}"
                    )
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "source-deletion-inventory-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(
                        page, engine, "source-deletion-inventory-forced-colors"
                    )
                    page.emulate_media(forced_colors="none")
                page.emulate_media(color_scheme="light")
                page.get_by_role("link", name="Next inventory page").click()
                expect(page.get_by_text("Page 2 of 3", exact=False)).to_be_visible()
                with page.expect_download() as event:
                    page.get_by_role(
                        "link", name="Download the complete deletion inventory (JSON)"
                    ).click()
                self.assertEqual(
                    event.value.suggested_filename, f"deletion-{source.pk}.json"
                )
                inventory = json.loads(Path(event.value.path()).read_bytes())
                self.assertEqual(len(inventory["entries"]), 117)
                self.assertTrue(
                    {str(obs.pk) for obs in observations}
                    <= {entry["id"] for entry in inventory["entries"]}
                )
                check = page.get_by_role("checkbox")
                check.focus()
                check.press("Space")
                check.press(
                    "Alt+Tab"
                    if engine == "webkit" and sys.platform == "darwin"
                    else "Tab"
                )
                expect(
                    page.get_by_role("link", name="Cancel", exact=True)
                ).to_be_focused()
                page.get_by_role("link", name="Cancel", exact=True).press("Enter")
                expect(page).to_have_url(url + "?info=1#evidence-details-heading")
                self.open_evidence_panel(page, "more")
                page.get_by_role("link", name="Delete evidence", exact=True).click()

                def add_late_observation(vehicle=vehicle, batch=batch):
                    return VehicleLocationEvidence.objects.create(
                        case=self.case,
                        uploaded_by=self.owner,
                        vehicle=vehicle,
                        title="Imported after review",
                        import_batch=batch,
                        source_row=100,
                        address="Synthetic late observation",
                    ).pk

                late_id = self.db(add_late_observation)
                page.get_by_role("checkbox").check()
                page.get_by_role("button", name="Permanently delete evidence").click()
                expect(
                    page.get_by_text("What will be removed has changed", exact=False)
                ).to_be_visible()
                expect(page.get_by_role("checkbox")).not_to_be_checked()
                self.assertTrue(
                    self.db(DocumentEvidence.objects.filter(pk=source.pk).exists)
                )
                self.capture(page, engine, "source-deletion-inventory-refreshed")
            with self.browser_page(engine, java_script_enabled=False) as (_, page):
                self.sign_in(page)
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(url + "?section=details")
                self.open_evidence_panel(page, "more")
                page.get_by_role("link", name="Delete evidence", exact=True).click()
                page.get_by_role("link", name="Next inventory page").click()
                page.get_by_role("checkbox").check()
                page.get_by_role("button", name="Permanently delete evidence").press(
                    "Enter"
                )
                expect(
                    page.get_by_text("Evidence deleted.", exact=True)
                ).to_be_visible()
                for model, identifier in (
                    (DocumentEvidence, source.pk),
                    (VehicleLocationImport, batch.pk),
                    (AnalysisRun, run.pk),
                    (AnalysisArtifact, artifact.pk),
                    (VehicleLocationEvidence, late_id),
                ):
                    self.assertFalse(
                        self.db(model.objects.filter(pk=identifier).exists)
                    )
                self.assertFalse(
                    self.db(
                        VehicleLocationEvidence.objects.filter(
                            import_batch_id=batch.pk
                        ).exists
                    )
                )
                self.assertFalse(
                    self.db(
                        VehicleLocationImportRow.objects.filter(
                            batch_id=batch.pk
                        ).exists
                    )
                )
                self.assertTrue(self.db(Vehicle.objects.filter(pk=vehicle.pk).exists))
                for original in (source.file, artifact.file):
                    self.assertFalse(original.storage.exists(original.name))

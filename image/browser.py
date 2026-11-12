"""Synthetic plate-search journey through the real local parser and private UI."""

import re
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin
from uuid import UUID

from django.core.files.base import ContentFile
from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from core.analysis.services import process_run
from image.models import ImageEvidence
from image.testing import plate_scene


class PlateBrowserTests(BrowserTestCase):
    artifact_subdirectory = "plate-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("plate-browser-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic plate case")
        self.original = plate_scene(distractors=True)
        self.image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic vehicle plate",
            file=ContentFile(self.original, name="synthetic-plate.jpeg"),
        )
        self.gateway = self.enterContext(
            patch("core.analysis.gateway.GatewayClient.request")
        )

    def test_queue_local_plate_search_inspect_crops_and_original_region(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:analysis:create", case_id=self.case.pk))
                page.get_by_label("Analysis", exact=True).select_option("plate_ocr")
                page.get_by_role("button", name="Show sources", exact=True).click()
                page.get_by_label("Image · Synthetic vehicle plate", exact=True).check()
                expect(
                    page.get_by_role("checkbox", name="I approve external processing")
                ).to_have_count(0)
                page.get_by_role("button", name="Queue analysis", exact=True).click()
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
                ).to_be_visible()
                run_id = UUID(page.url.rstrip("/").split("/")[-1])
                self.db(process_run, run_id)
                page.reload()
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Partial$"))
                ).to_be_visible()
                page.goto(
                    self.url(
                        "core:analysis:process_evidence",
                        case_id=self.case.pk,
                        kind="image",
                        evidence_id=self.image.pk,
                    )
                )
                plate_text = re.compile(r"^TEST 482\b", re.IGNORECASE)
                finding = page.get_by_role("region", name="Findings").get_by_role(
                    "link", name=plate_text
                )
                expect(finding).to_have_count(1)
                expect(finding).to_contain_text("Awaiting review")
                expect(finding).to_contain_text("Partial coverage")
                expect(finding).to_contain_text("Plate-likeness:")
                self.assertEqual(
                    finding.evaluate("node => getComputedStyle(node).display"), "flex"
                )
                self.assertEqual(
                    page.locator(".analysis-finding-rows").evaluate(
                        "node => getComputedStyle(node).listStyleType"
                    ),
                    "none",
                )
                expect(page.get_by_role("heading", name="Run history")).to_be_visible()
                for width, height in ((1440, 900), (390, 844), (320, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"plate-highlights-{width}x{height}")
                finding.focus()
                expect(finding).to_be_focused()
                self.assertNotEqual(
                    finding.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                finding.press("Enter")
                expect(page.locator(".analysis-finding-text")).to_contain_text(
                    plate_text
                )
                preview = page.locator(".analysis-plate-preview")
                expect(preview).to_be_visible()
                response = page.request.get(
                    urljoin(page.url, preview.get_attribute("src"))
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["content-type"], "image/jpeg")
                self.assertIn("private", response.headers["cache-control"])
                self.assertIn("no-store", response.headers["cache-control"])
                region = page.get_by_text("Region on source image", exact=True)
                source_image = page.get_by_role(
                    "img", name="Source image with the possible plate region marked"
                )
                expect(source_image).to_be_visible()
                region.focus()
                expect(region).to_be_focused()
                region.press("Space")
                expect(source_image).to_be_hidden()
                region.press("Space")
                expect(source_image).to_be_visible()
                page.set_viewport_size({"width": 1440, "height": 900})
                page.get_by_role("link", name="Back to findings", exact=True).click()
                expect(page.locator(".analysis-finding-link")).to_have_count(3)
                toggle = page.get_by_role("link", name="Show weak regions", exact=True)
                toggle.focus()
                expect(toggle).to_be_focused()
                toggle.press("Enter")
                expect(page.locator(".analysis-finding-link")).to_have_count(15)
                page.locator("#finding-link-1").click()
                expect(
                    page.get_by_text(
                        "This region is below the default filter threshold.",
                        exact=False,
                    )
                ).to_be_visible()
                self.capture(page, engine, "plate-weak-regions")
                page.get_by_role("link", name="Hide weak regions", exact=True).click()
                expect(page.locator(".analysis-finding-link")).to_have_count(3)
                page.locator(".analysis-finding-link").first.click()
                expect(
                    page.get_by_text("Heuristic score, not a probability.", exact=False)
                ).to_be_visible()
                expect(source_image).to_be_visible()
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
                    preview.scroll_into_view_if_needed()
                    self.capture(page, engine, f"plate-reading-{width}x{height}")
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "plate-reading-dark")
                page.emulate_media(forced_colors="active", reduced_motion="reduce")
                self.capture(page, engine, "plate-reading-forced-colors")
                page.goto(
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="image",
                        evidence_id=self.image.pk,
                    )
                )
                with page.expect_download() as waiting:
                    page.get_by_role(
                        "link", name="Download original", exact=True
                    ).first.click()
                self.assertEqual(Path(waiting.value.path()).read_bytes(), self.original)
        self.gateway.assert_not_called()

"""Synthetic plate-search journey through the real local parser and private UI."""

import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin
from uuid import UUID

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.base import ContentFile
from django.test import override_settings
from playwright.sync_api import expect

from cases.models import Case
from config.tests import browser as core_browser
from core.analysis.services import process_run
from image.models import ImageEvidence
from image.testing import plate_scene


@override_settings(
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
    ACCOUNT_RATE_LIMITS=False,
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
)
class PlateBrowserTests(StaticLiveServerTestCase):
    password = "synthetic-browser-correct-horse-742!"
    make_user = core_browser.CoreInterfaceBrowserTests.make_user
    url = core_browser.CoreInterfaceBrowserTests.url
    db = core_browser.CoreInterfaceBrowserTests.db
    browser_page = core_browser.CoreInterfaceBrowserTests.browser_page
    sign_in = core_browser.CoreInterfaceBrowserTests.sign_in
    capture = core_browser.CoreInterfaceBrowserTests.capture

    def setUp(self):
        super().setUp()
        self.playwright = None
        self.browsers = {}
        self.browser_context_number = 0
        self.database_worker = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.database_worker.shutdown)
        self.media_directory = tempfile.TemporaryDirectory(
            prefix="pandect-plate-browser-"
        )
        self.addCleanup(self.media_directory.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=self.media_directory.name))
        self.artifacts = Path(
            os.environ.get("PANDECT_BROWSER_ARTIFACTS", "data/validation/plate-browser")
        ).resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.owner = self.make_user("plate-browser-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic plate case")
        self.original = plate_scene()
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
        engines = tuple(
            os.environ.get("PANDECT_BROWSER_ENGINES", "chromium,webkit").split(",")
        )
        for engine in engines:
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
                plate_text = re.compile(r"\btest 482\b", re.IGNORECASE)
                page.locator(".analysis-finding-link").filter(
                    has_text=plate_text
                ).first.click()
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
                region = page.locator("summary").filter(
                    has_text="Show region on source image"
                )
                region.focus()
                expect(region).to_be_focused()
                region.press("Space")
                expect(
                    page.get_by_role(
                        "img", name="Source image with the possible plate region marked"
                    )
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

"""Complete an evidence packet in the browser without JavaScript."""

from pathlib import Path

from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from text.models import TextEvidence


class EvidencePacketBrowserTests(BrowserTestCase):
    artifact_subdirectory = "evidence-packet"

    def test_review_and_download_with_keyboard(self):
        self.owner = self.make_user("packet-owner")
        case = Case.objects.create(owner=self.owner, name="Private packet case")
        TextEvidence.objects.create(
            case=case,
            uploaded_by=self.owner,
            title="Private interview",
            body="Synthetic person's name appeared here.",
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:evidence:list", case_id=case.pk))
                page.get_by_role("link", name="Prepare evidence packet").click()
                page.get_by_role("checkbox", name="Private interview · Text").check()
                page.get_by_role("button", name="Add selected sources").press("Enter")
                page.get_by_role("link", name="Prepare exhibits").click()
                page.get_by_label("Packet title").fill("Review copy")
                page.get_by_label("Exhibit title").fill("Interview excerpt")
                page.get_by_label("Role in the packet").select_option("contrary")
                page.get_by_label("Source citation").fill("Interview, line 1")
                page.get_by_label("Text to disclose").fill(
                    "[Name omitted]\nThe witness could not confirm it."
                )
                page.get_by_role("button", name="Preview packet").press("Enter")
                expect(
                    page.get_by_role("heading", name="Review evidence packet")
                ).to_be_visible()
                expect(page.get_by_text("Contrary material · Undated")).to_be_visible()
                page.get_by_role("button", name="Edit packet").press("Enter")
                expect(page.get_by_label("Text to disclose")).to_have_value(
                    "[Name omitted]\nThe witness could not confirm it."
                )
                page.get_by_role("button", name="Preview packet").press("Enter")
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate(
                            "document.documentElement.scrollWidth - innerWidth"
                        ),
                        1,
                    )
                    self.capture(page, engine, f"packet-preview-{width}x{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "packet-preview-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "packet-preview-forced-colors")
                button = page.get_by_role("button", name="Download evidence packet")
                button.focus()
                expect(button).to_be_focused()
                with page.expect_download() as event:
                    button.press("Enter")
                download = event.value
                self.assertEqual(download.suggested_filename, "evidence-packet.html")
                content = Path(download.path()).read_text(encoding="utf-8")
                self.assertIn(
                    "[Name omitted]\nThe witness could not confirm it.", content
                )
                self.assertNotIn("Synthetic person's name", content)
                self.assertNotIn("Private packet case", content)

"""Explicit browser journey for debug-only admin bulk deletion."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from document.models import DocumentEvidence


@override_settings(DEBUG=True)
class BulkEvidenceAdminBrowserTests(BrowserTestCase):
    def test_review_cancel_and_confirm_without_javascript(self):
        self.owner = self.make_user("bulk-admin")
        self.owner.is_staff = self.owner.is_superuser = True
        self.owner.save(update_fields=["is_staff", "is_superuser"])
        case = Case.objects.create(owner=self.owner, name="Synthetic bulk cleanup")
        other_case = Case.objects.create(owner=self.owner, name="Other cleanup case")
        for engine in self.engines:
            items = [
                DocumentEvidence.objects.create(
                    case=case if number == 0 else other_case,
                    uploaded_by=self.owner,
                    title=f"Synthetic source {engine} {number}",
                    file=SimpleUploadedFile("original.txt", b"Synthetic original"),
                )
                for number in range(2)
            ]
            url = self.url("admin:document_documentevidence_changelist")
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)

                def review(selected_items, changelist_url):
                    page.goto(changelist_url)
                    for item in selected_items:
                        page.locator(
                            f'input[name="_selected_action"][value="{item.pk}"]'
                        ).check()
                    page.locator('select[name="action"]').select_option(
                        "delete_selected"
                    )
                    page.get_by_role("button", name="Run", exact=True).click()
                    expect(
                        page.get_by_role(
                            "heading", name="Delete selected evidence", exact=True
                        )
                    ).to_be_visible()

                review(items, url)
                expect(
                    page.get_by_text("2 records and 2 stored files will be deleted.")
                ).to_be_visible()
                expect(
                    page.get_by_role("rowheader", name=case.name, exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("rowheader", name=other_case.name, exact=True)
                ).to_be_visible()
                expect(page.get_by_text(items[0].title, exact=True)).to_have_count(0)
                page.get_by_role("button", name="Cancel", exact=True).click()
                self.assertEqual(self.db(DocumentEvidence.objects.count), 2)
                review(items, url)
                confirm = page.get_by_role(
                    "button", name="Permanently delete selected evidence", exact=True
                )
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    confirm.scroll_into_view_if_needed()
                    self.assertLessEqual(
                        page.evaluate(
                            "document.documentElement.scrollWidth - innerWidth"
                        ),
                        1,
                    )
                    self.capture(page, engine, f"bulk-delete-{width}-{height}")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth - innerWidth"),
                    1,
                )
                # Platform font metrics must not make branding overflow at 200%.
                branding = page.locator("#site-name")
                branding.evaluate("node => node.style.fontFamily = 'monospace'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth - innerWidth"),
                    1,
                )
                self.capture(page, engine, "bulk-delete-large-text-wide-font")
                branding.evaluate("node => node.style.fontFamily = ''")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "bulk-delete-dark")
                if engine == "chromium":
                    page.emulate_media(forced_colors="active")
                    self.capture(page, engine, "bulk-delete-forced-colors")
                checkbox = page.get_by_role(
                    "checkbox", name="I confirm permanent deletion.", exact=True
                )
                checkbox.focus()
                checkbox.press("Space")
                confirm.focus()
                expect(confirm).to_be_focused()
                self.assertNotEqual(
                    confirm.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                self.assertGreaterEqual(confirm.bounding_box()["height"], 44)
                confirm.press("Enter")
                expect(
                    page.get_by_text(
                        "Permanently deleted 2 evidence records.", exact=True
                    )
                ).to_be_visible()
                self.assertEqual(self.db(DocumentEvidence.objects.count), 0)
                for item in items:
                    self.assertFalse(item.file.storage.exists(item.file.name))

                single = self.db(
                    DocumentEvidence.objects.create,
                    case=case,
                    uploaded_by=self.owner,
                    title="Synthetic single source",
                    file=SimpleUploadedFile("single.txt", b"Synthetic original"),
                )
                single_url = self.url(
                    "admin:document_documentevidence_delete", object_id=single.pk
                )
                page.goto(single_url)
                expect(
                    page.get_by_role("heading", name="Deletion summary")
                ).to_be_visible()
                expect(
                    page.get_by_role("rowheader", name=case.name, exact=True)
                ).to_be_visible()
                for width, height in ((1440, 900), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate(
                            "document.documentElement.scrollWidth - innerWidth"
                        ),
                        1,
                    )
                    self.capture(page, engine, f"single-delete-{width}-{height}")
                page.get_by_role("button", name="Cancel", exact=True).click()
                self.assertEqual(self.db(DocumentEvidence.objects.count), 1)
                page.goto(single_url)
                page.get_by_role(
                    "button", name="Permanently delete evidence", exact=True
                ).click()
                self.assertEqual(self.db(DocumentEvidence.objects.count), 0)

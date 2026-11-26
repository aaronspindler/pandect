"""Short workspace forms keep the investigator at the originating record."""

from django.utils import timezone
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.case_search.tests.fixtures import indexed
from core.models import ImportantItem, ImportantNote
from people.models import Person
from text.models import TextEvidence


class QuickFormBrowserTests(BrowserTestCase):
    artifact_subdirectory = "workspace-dialogs"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("dialog-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic case")

    def test_opening_a_dialog_supersedes_a_pending_rail_visit(self):
        listing = self.url("people:list", case_id=self.case.pk)
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(listing)
                stalled = []
                rail = page.get_by_role("link", name="Processing", exact=True)
                destination = rail.evaluate("link => link.href")

                def hold(route, _request, stalled=stalled):
                    stalled.append(route)

                page.route(destination, hold)
                with page.expect_request(destination):
                    rail.click()
                driver = page.locator("[data-workspace-status]")
                expect(driver).to_have_attribute("aria-busy", "true")
                trigger = page.get_by_role("link", name="Add person")
                trigger.click()
                dialog = page.get_by_role("dialog", name="Add person")
                expect(dialog.get_by_label("First name")).to_be_visible()
                expect(driver).not_to_have_attribute("aria-busy", "true")
                dialog.get_by_role("button", name="Close").click()
                stalled[0].continue_()
                expect(trigger).to_be_focused()
                expect(page).to_have_url(listing)

    def test_evidence_edit_updates_metadata_and_list_without_replacing_original(self):
        for engine in self.engines:
            source = TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Draft {engine}",
                body="Unaltered original.",
            )
            url = self.url(
                "core:evidence:detail",
                case_id=self.case.pk,
                kind="text",
                evidence_id=source.pk,
            )
            with (
                self.subTest(engine=engine),
                indexed(self.case, [("original", "text", source.pk)]),
                self.browser_page(engine) as (_, page),
            ):
                self.sign_in(page)
                page.goto(url + f"?type=text&q=Draft+{engine}")
                page.evaluate(
                    "window.originalNode = document.querySelector('.original-text')"
                )
                trigger = page.get_by_role("link", name="Edit details")
                trigger.click()
                dialog = page.get_by_role("dialog", name="Edit evidence details")
                dialog.get_by_label("Title", exact=True).fill("Retained title")
                dialog.get_by_role("button", name="Save changes").click()
                expect(
                    page.get_by_role("heading", name="Retained title")
                ).to_be_visible()
                expect(
                    page.get_by_role("heading", name="No matching evidence")
                ).to_be_visible()
                expect(trigger).to_be_focused()
                self.assertTrue(page.evaluate("window.originalNode.isConnected"))

    def test_person_edit_preserves_draft_focus_and_location(self):
        for engine in self.engines:
            person = Person.objects.create(case=self.case, first_name="Witness")
            url = self.url("people:detail", case_id=self.case.pk, person_id=person.pk)
            with (
                self.subTest(engine=engine),
                indexed(self.case, [("record", "person", person.pk)]),
                self.browser_page(engine) as (_, page),
            ):
                self.sign_in(page)
                listing = self.url("people:list", case_id=self.case.pk)
                page.goto(listing + "?q=Witness&page=2")
                page.locator(f'a.collection-row[href*="{person.pk}"]').click()
                expect(page).to_have_url(url + "?q=Witness&page=2")
                page.evaluate("window.dialogVisit = true")
                trigger = page.get_by_role("link", name="Edit person")
                trigger.press("Enter")
                dialog = page.get_by_role("dialog", name="Edit person")
                first_name = dialog.get_by_label("First name")
                expect(first_name).to_be_focused()
                close = dialog.get_by_role("button", name="Close")
                cancel = dialog.get_by_role("link", name="Cancel")
                close.focus()
                close.press("Shift+Tab")
                expect(cancel).to_be_focused()
                cancel.press("Shift+Tab")
                save = dialog.get_by_role("button", name="Save changes")
                expect(save).to_be_focused()
                save.press("Tab")
                expect(cancel).to_be_focused()
                cancel.press("Tab")
                expect(close).to_be_focused()
                close.press("Tab")
                expect(first_name).to_be_focused()
                first_name.fill("Revised witness")
                page.once("dialog", lambda prompt: prompt.dismiss())
                with page.expect_event("dialog"):
                    page.evaluate("setTimeout(() => history.back(), 0)")
                expect(dialog).to_be_visible()
                expect(first_name).to_have_value("Revised witness")
                expect(page).to_have_url(url + "?q=Witness&page=2")
                page.once("dialog", lambda prompt: prompt.accept())
                first_name.press("Escape")
                expect(trigger).to_be_focused()
                trigger.press("Enter")
                expect(first_name).to_have_value("Witness")
                first_name.fill("Updated witness")
                dialog.get_by_role("button", name="Save changes").click()
                expect(
                    page.get_by_role("heading", name="Updated witness")
                ).to_be_visible()
                expect(trigger).to_be_focused()
                expect(page).to_have_url(url + "?q=Witness&page=2")
                self.assertTrue(page.evaluate("window.dialogVisit"))

    def test_note_returns_to_marked_item_and_revoked_editor_cannot_write(self):
        editor = self.make_user("dialog-editor")
        for engine in self.engines:
            membership = CaseMembership.objects.create(
                case=self.case, user=editor, role="editor"
            )
            source = TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Source {engine}",
                body="Synthetic original",
            )
            item = ImportantItem.objects.create(
                case=self.case,
                marked_by=self.owner,
                marked_at=timezone.now(),
                evidence_kind="text",
                evidence_id=source.pk,
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page, editor)
                url = self.url("core:important:list", case_id=self.case.pk)
                page.goto(url)
                page.set_viewport_size({"width": 320, "height": 844})
                row = page.locator(f"#important-{item.pk}")
                trigger = row.get_by_role("link", name="Add note")
                trigger.click()
                dialog = page.get_by_role("dialog", name="Add note")
                note = dialog.get_by_label("Note", exact=True)
                expect(note).to_be_focused()
                note.fill("Review this source.")
                self.assertLessEqual(
                    dialog.evaluate("el => el.scrollWidth - el.clientWidth"), 1
                )
                self.capture(page, engine, "add-note-mobile")
                dialog.get_by_role("button", name="Add note").click()
                expect(
                    row.get_by_role("link", name=f"Notes for {source.title} (1)")
                ).to_be_visible()
                expect(trigger).to_be_focused()
                expect(page).to_have_url(url)

                trigger.click()
                note.fill("Cannot save this draft.")
                self.db(membership.delete)
                dialog.get_by_role("button", name="Add note").click()
                expect(page.get_by_role("link", name="All cases")).to_be_visible()
                expect(dialog).to_have_count(0)
                expect(note).to_have_count(0)
                self.assertEqual(
                    self.db(ImportantNote.objects.filter(item=item).count), 1
                )

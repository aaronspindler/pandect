"""Synthetic Important journey with ordinary navigation and forms."""

import re
from urllib.parse import parse_qs, urlsplit

from django.utils import timezone
from playwright.sync_api import expect

from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.navigation import finding_url
from core.analysis.services import process_run, start_run
from core.models import ImportantItem
from text.models import TextEvidence


class ImportantBrowserTests(BrowserTestCase):
    artifact_subdirectory = "important-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("owner")
        self.viewer = self.make_user("viewer")

    def _make_case(self, name):
        case = Case.objects.create(owner=self.owner, name=name)
        CaseMembership.objects.create(case=case, user=self.viewer)
        source = TextEvidence.objects.create(
            case=case,
            uploaded_by=self.owner,
            title="Harbour correspondence",
            body="Send to first@example.test or second@example.test.",
        )
        run, _ = start_run(
            self.owner, case.pk, "entities", [{"kind": "text", "id": source.pk}]
        )
        process_run(run.pk)
        return case, source, run

    def _fill_first_page(self, case):
        items = [
            TextEvidence.objects.create(
                case=case,
                title=f"Supplemental evidence {number}",
                body="Synthetic supporting material",
                uploaded_by=self.owner,
            )
            for number in range(25)
        ]
        ImportantItem.objects.bulk_create(
            [
                ImportantItem(
                    case=case,
                    evidence_kind="text",
                    evidence_id=item.pk,
                    marked_by=self.owner,
                    marked_at=timezone.now(),
                )
                for item in items
            ]
        )

    def _assert_reflow(self, page):
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"),
            page.evaluate("document.documentElement.clientWidth") + 1,
        )

    def test_evidence_actions_retain_live_filters(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                case, source, _ = self._make_case(f"Evidence filter case ({engine})")
                self._walk_through_evidence_filters(engine, case, source)

    def _walk_through_evidence_filters(self, engine, case, source):
        detail = self.url(
            "core:evidence:detail", case_id=case.pk, kind="text", evidence_id=source.pk
        )
        filters = {"type": ["text"], "state": ["all"]}
        note = "Review the correspondence together with its attachment."
        with self.browser_page(engine) as (_, page):
            self.sign_in(page)
            page.goto(f"{detail}?type=text&state=all")
            search = page.get_by_role("searchbox", name="Filter evidence")
            search.fill("Harbour")
            expect(page).to_have_url(re.compile(r"[?&]q=Harbour(?:[&#]|$)"))
            self.open_evidence_panel(page, "more")
            page.get_by_role("button", name="Mark important", exact=True).press("Enter")
            notes = page.get_by_role("link", name="Notes", exact=True)
            expect(notes).to_be_visible()
            self.assertEqual(
                parse_qs(urlsplit(page.url).query), filters | {"q": ["Harbour"]}
            )

            search.fill("correspondence")
            expect(page).to_have_url(re.compile(r"[?&]q=correspondence(?:[&#]|$)"))
            notes.press("Enter")
            page.get_by_role("textbox", name="Note", exact=True).fill(note)
            page.get_by_role("button", name="Add note", exact=True).press("Enter")
            expect(page.get_by_text(note, exact=True)).to_be_visible()
            page.get_by_role("link", name="Open source", exact=True).press("Enter")
            expect(
                page.get_by_role("heading", name=source.title, exact=True)
            ).to_be_visible()
            self.assertEqual(
                parse_qs(urlsplit(page.url).query), filters | {"q": ["correspondence"]}
            )

            search.fill("")
            expect(page).not_to_have_url(re.compile(r"[?&]q="))
            self.open_evidence_panel(page, "more")
            page.get_by_role("button", name="Remove from Important", exact=True).press(
                "Enter"
            )
            expect(
                page.get_by_text("Removed from Important.", exact=True)
            ).to_be_visible()
            self.assertEqual(
                parse_qs(urlsplit(page.url).query, keep_blank_values=True), filters
            )

    def test_mark_note_reopen_and_return_without_javascript(self):
        for engine in self.engines:
            with self.subTest(engine=engine):
                case, source, run = self._make_case(f"Harbour case ({engine})")
                self._walk_through_important(engine, case, source, run)

    def _walk_through_important(self, engine, case, source, run):
        note = "Check the correspondence against the second address."
        with self.browser_page(engine, java_script_enabled=False) as (_, page):
            self.sign_in(page)
            page.goto(
                self.url(
                    "core:evidence:detail",
                    case_id=case.pk,
                    kind="text",
                    evidence_id=source.pk,
                )
            )
            self.open_evidence_panel(page, "more")
            page.get_by_role("button", name="Mark important", exact=True).press("Enter")
            expect(
                page.get_by_role("heading", name=source.title, exact=True)
            ).to_be_visible()
            page.goto(self.live_server_url + finding_url(case.pk, run.pk, 1))
            page.get_by_role("button", name="Mark important", exact=True).press("Enter")
            expect(page.locator("#finding-2")).to_be_visible()
            self.db(self._fill_first_page, case)

            page.goto(self.url("core:analysis:index", case_id=case.pk))
            page.get_by_role("link", name="Important", exact=True).press("Enter")
            expect(
                page.get_by_role("heading", name="Important", exact=True)
            ).to_be_visible()
            next_page = page.get_by_role("link", name="Next", exact=True)
            next_box = next_page.bounding_box()
            self.assertGreaterEqual(next_box["width"], 44)
            self.assertGreaterEqual(next_box["height"], 44)
            next_page.press("Enter")
            expect(page).to_have_url(re.compile(r"[?&]page=2"))
            previous_box = page.get_by_role(
                "link", name="Previous", exact=True
            ).bounding_box()
            self.assertGreaterEqual(previous_box["width"], 44)
            self.assertGreaterEqual(previous_box["height"], 44)
            finding_id = self.db(
                lambda: (
                    ImportantItem.objects.get(finding_result__run=run, item_index=1).pk
                )
            )
            finding_row = page.locator(f"#important-{finding_id}")
            finding_link = finding_row.locator(f'a[href*="{run.pk}"]')
            expect(finding_link).to_have_count(1)
            finding_link.press("Enter")
            self.assertEqual(parse_qs(urlsplit(page.url).query)["finding"], ["2"])
            expect(page.locator("#finding-2")).to_be_visible()
            page.get_by_role("link", name="Back to Important", exact=True).press(
                "Enter"
            )
            expect(page).to_have_url(re.compile(rf"[?&]page=2#important-{finding_id}$"))
            source_id = self.db(
                lambda: ImportantItem.objects.get(case=case, evidence_id=source.pk).pk
            )
            page.locator(f"#important-{source_id}").get_by_role(
                "link", name=source.title, exact=True
            ).press("Enter")
            expect(
                page.get_by_role("heading", name=source.title, exact=True)
            ).to_be_visible()
            page.get_by_role("link", name="Back to Important", exact=True).press(
                "Enter"
            )
            expect(page).to_have_url(re.compile(rf"[?&]page=2#important-{source_id}$"))

            for width, height in (
                (1440, 900),
                (1440, 1024),
                (768, 1024),
                (390, 844),
                (320, 844),
            ):
                page.set_viewport_size({"width": width, "height": height})
                self._assert_reflow(page)
                self.capture(page, engine, f"important-{width}-{height}")
            page.set_viewport_size({"width": 1440, "height": 900})
            page.evaluate("document.documentElement.style.fontSize = '200%'")
            self._assert_reflow(page)
            page.evaluate("document.documentElement.style.removeProperty('font-size')")
            page.set_viewport_size({"width": 390, "height": 844})
            finding_notes = finding_row.get_by_role(
                "link", name=re.compile("^Notes for ")
            )
            finding_notes.press("Enter")
            note_field = page.get_by_role("textbox", name="Note", exact=True)
            note_field.focus()
            expect(note_field).to_be_focused()
            note_field.fill(note)
            page.get_by_role("button", name="Add note", exact=True).press("Enter")
            expect(page.get_by_text(note, exact=True)).to_be_visible()
            self._assert_reflow(page)
            self.capture(page, engine, "important-notes-mobile")
            page.emulate_media(color_scheme="dark", reduced_motion="reduce")
            self.capture(page, engine, "important-notes-dark")
            if engine == "chromium":
                page.emulate_media(forced_colors="active")
                self.capture(page, engine, "important-notes-forced-colors")
            notes_url = page.url

        with self.browser_page(engine, java_script_enabled=False) as (_, page):
            self.sign_in(page, self.viewer)
            page.goto(notes_url)
            expect(page.get_by_text(note, exact=True)).to_be_visible()
            expect(
                page.get_by_role("button", name="Add note", exact=True)
            ).to_have_count(0)

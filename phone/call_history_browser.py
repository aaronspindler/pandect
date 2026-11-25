"""Keyboard-accessible call history import with a no-JavaScript fallback."""

from urllib.parse import urlsplit
from uuid import UUID

from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from phone.call_history_jobs import process_call_history
from phone.models import CallHistoryImport
from phone.tests.test_call_import_views import call_history_csv


def _run_import(batch_id):
    batch = CallHistoryImport.objects.get(pk=batch_id)
    return process_call_history.func(str(batch.pk), str(batch.attempt_id))


class CallHistoryBrowserTests(BrowserTestCase):
    artifact_subdirectory = "call-history-browser"

    def test_publish_ready_calls_and_inspect_held_rows_without_javascript(self):
        self.owner = self.make_user("call-history-owner")
        case = Case.objects.create(owner=self.owner, name="Synthetic call history")
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:evidence:create", case_id=case.pk))
                page.get_by_role(
                    "link", name="Import call history CSV", exact=True
                ).click()
                page.get_by_label("Call history CSV", exact=True).set_input_files(
                    {
                        "name": "synthetic-calls.csv",
                        "mimeType": "text/csv",
                        "buffer": call_history_csv(
                            {"Number": ""},
                            {"Date": "2025-11-02 01:30:00"},
                        ),
                    }
                )
                page.get_by_label("New source label").fill(f"Synthetic {engine} line")
                page.get_by_label("Export timezone", exact=True).fill("America/Toronto")
                page.get_by_label("I confirm this is the export timezone").check()
                page.get_by_role(
                    "button", name="Prepare call history", exact=True
                ).click()
                expect(
                    page.get_by_text("Queued for processing", exact=False)
                ).to_be_visible()
                batch_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                self.assertEqual(self.db(_run_import, batch_id), "review")
                page.get_by_role("link", name="Refresh status", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Review call history")
                ).to_be_visible()
                page.get_by_role(
                    "link", name="Rows held for review", exact=True
                ).click()
                expect(
                    page.get_by_text("This date occurs twice in the export timezone.")
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
                    self.capture(page, engine, f"held-{width}-{height}")
                publish = page.get_by_role("button", name="Publish calls", exact=True)
                publish.focus()
                expect(publish).to_be_focused()
                publish.press("Enter")
                self.assertEqual(self.db(_run_import, batch_id), "completed")
                page.get_by_role("link", name="Refresh status", exact=True).click()
                expect(
                    page.get_by_text("1 published · 0 skipped · 1 held for review")
                ).to_be_visible()

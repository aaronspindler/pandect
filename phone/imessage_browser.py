"""No-JavaScript review, deliberate person creation, and responsive message intake."""

import re
from urllib.parse import urlsplit
from uuid import UUID

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from playwright.sync_api import expect

from cases.models import Case
from config.tests.browser_base import BrowserTestCase
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.services import process_run, start_run
from core.models import AnalysisRun, CaseAnalysisBatch
from document.models import DocumentEvidence
from people.models import Person
from phone.models import TextMessageEvidence
from phone.tests.test_imessage_views import (
    SYNTHETIC_MESSAGE_GATEWAY,
    SYNTHETIC_MESSAGE_LOCAL,
    message_csv,
)


class IMessageImportBrowserTests(BrowserTestCase):
    artifact_subdirectory = "message-csv-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("imessage-browser-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic direct chat")

    @override_settings(PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY)
    def test_no_javascript_upload_review_people_and_import(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("phone:imessage_upload", case_id=self.case.pk))
                page.get_by_label("iMessage CSV", exact=True).set_input_files(
                    {
                        "name": "synthetic-messages.csv",
                        "mimeType": "text/csv",
                        "buffer": message_csv(
                            {},
                            {"Type": "Outgoing", "Sender ID": "", "Sender Name": ""},
                            {"Message Date": "2026-07-30 00:00:00", "Text": "Later"},
                        ),
                    }
                )
                page.get_by_label("Export timezone", exact=True).fill("America/Toronto")
                page.get_by_label(
                    "I confirm this is the export's timezone", exact=True
                ).check()
                start_date = page.get_by_label("Start date", exact=True)
                expect(start_date).to_be_hidden()
                page.get_by_text("Limit by date", exact=True).press("Enter")
                start_date.focus()
                expect(start_date).to_be_focused()
                start_date.fill("2026-07-29")
                page.get_by_label("End date", exact=True).fill("2026-07-29")
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
                    self.capture(page, engine, f"upload-date-range-{width}-{height}")
                page.get_by_role(
                    "button", name="Prepare message review", exact=True
                ).click()
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
                ).to_be_visible()
                preview_id = UUID(page.url.rstrip("/").split("/")[-1])
                self.db(process_run, preview_id)
                page.reload()
                expect(
                    page.get_by_text("1 outside date range", exact=False)
                ).to_be_visible()
                expect(page.get_by_text("2026-07-29 to 2026-07-29")).to_be_visible()
                expect(
                    page.get_by_role("heading", name="Message preview", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("region", name="Message preview")
                ).to_contain_text("Original café")
                page.get_by_role(
                    "link", name="Review all message rows", exact=True
                ).click()
                expect(
                    page.get_by_text("Showing 1–3 of 3 rows.", exact=False)
                ).to_be_visible()
                expect(page.locator("#imessage-message-rows")).to_contain_text(
                    "Original café"
                )
                account = page.get_by_role(
                    "group", name="Export account — outgoing sender", exact=True
                )
                account.get_by_label("Person", exact=True).select_option("create")
                if not account.get_by_label("First name", exact=True).is_visible():
                    account.get_by_text("New person details", exact=True).click()
                account.get_by_label("First name", exact=True).fill("Account sender")
                correspondent = page.get_by_role(
                    "group", name="Correspondent — incoming sender", exact=True
                )
                correspondent.get_by_label("Person", exact=True).select_option("create")
                if not correspondent.get_by_label(
                    "First name", exact=True
                ).is_visible():
                    correspondent.get_by_text("New person details", exact=True).click()
                correspondent.get_by_label("First name", exact=True).fill(
                    "Incoming sender"
                )
                expect(
                    correspondent.get_by_label("Phone number", exact=True)
                ).to_have_value("17055550100")
                before = self.db(Person.objects.count)
                confirm = page.get_by_label(
                    "I confirm this export is a direct conversation "
                    "between these two people",
                    exact=True,
                )
                confirm.focus()
                expect(confirm).to_be_focused()
                page.keyboard.press("Space")
                page.get_by_label(
                    "Import the ready messages with these people "
                    "and the confirmed timezone",
                    exact=True,
                ).check()
                local = page.get_by_role("radio", name="Local checks", exact=True)
                ai = page.get_by_role("radio", name="AI only", exact=True)
                both = page.get_by_role("radio", name="Local checks and AI", exact=True)
                expect(local).to_be_checked()
                expect(ai).not_to_be_checked()
                both.focus()
                expect(both).to_be_focused()
                both.press("Space")
                expect(both).to_be_checked()
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
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"ready-{width}-{height}")
                    if (width, height) in ((1440, 900), (390, 844)):
                        page.screenshot(
                            path=str(
                                self.artifacts
                                / f"{engine}-ready-{width}-{height}-full.png"
                            ),
                            full_page=True,
                            animations="disabled",
                        )
                page.get_by_role("button", name="Import messages", exact=True).click()
                expect(page.get_by_role("alert")).to_contain_text(
                    "Approve external processing to continue."
                )
                expect(both).to_be_checked()
                expect(account.get_by_label("First name", exact=True)).to_have_value(
                    "Account sender"
                )
                expect(
                    correspondent.get_by_label("First name", exact=True)
                ).to_have_value("Incoming sender")
                self.assertEqual(self.db(Person.objects.count), before)
                self.capture(page, engine, "external-approval-required")
                approval = page.get_by_label(
                    "I approve external AI processing of the newly imported messages",
                    exact=True,
                )
                approval.focus()
                expect(approval).to_be_focused()
                approval.press("Space")
                expect(approval).to_be_checked()
                account.get_by_label("First name", exact=True).fill("")
                page.get_by_role("button", name="Import messages", exact=True).click()
                expect(page.get_by_role("alert")).to_be_visible()
                expect(
                    correspondent.get_by_label("First name", exact=True)
                ).to_have_value("Incoming sender")
                self.assertEqual(self.db(Person.objects.count), before)
                self.capture(page, engine, "review-error-preserved")
                account.get_by_label("First name", exact=True).fill("Account sender")
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
                    self.capture(page, engine, f"review-{width}-{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "review-dark-reduced-motion")
                page.emulate_media(forced_colors="active")
                confirm.focus()
                expect(confirm).to_be_focused()
                self.capture(page, engine, "review-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                page.get_by_role("button", name="Import messages", exact=True).click()
                page.wait_for_url(re.compile(r"/imessage/[0-9a-f-]+/(?:#.*)?$"))
                final_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                self.assertEqual(self.db(Person.objects.count), before)
                self.db(process_run, final_id)
                page.reload()
                expect(
                    page.get_by_role("heading", name="Messages imported", exact=True)
                ).to_be_visible()
                final = self.db(AnalysisRun.objects.get, pk=final_id)
                self.assertEqual(final.status, "complete", final.error_code)
                self.assertEqual(final.parameters["analysis"]["mode"], "both")
                batch = self.db(CaseAnalysisBatch.objects.get, scope__id=str(final_id))
                self.assertEqual(batch.scope["mode"], "both")
                expect(
                    page.get_by_text(
                        "Selected analysis: Local checks and AI", exact=True
                    )
                ).to_be_visible()
                self.assertEqual(self.db(Person.objects.count), before + 2)
                self.assertEqual(
                    self.db(TextMessageEvidence.objects.filter(case=self.case).count),
                    2 * (self.engines.index(engine) + 1),
                )
                follow = page.get_by_role(
                    "link", name="Follow message analysis", exact=True
                )
                expect(follow).to_be_visible()
                expect(
                    page.get_by_role(
                        "button", name="Run local checks on imported evidence"
                    )
                ).to_have_count(0)
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
                    self.capture(page, engine, f"automatic-analysis-{width}-{height}")
                page.set_viewport_size({"width": 390, "height": 844})
                page.get_by_role("link", name="View conversation", exact=True).click()
                expect(page.locator(".conversation-stream")).to_be_visible()
                expect(page.locator("[data-message-panel]")).to_be_hidden()
                expect(
                    page.locator(f"#{urlsplit(page.url).fragment}")
                ).to_be_in_viewport()
                self.capture(page, engine, "conversation-after-import-mobile")
                page.go_back()
                follow.focus()
                expect(follow).to_be_focused()
                page.keyboard.press("Enter")
                expect(page).to_have_url(
                    self.url(
                        "phone:imessage_analysis_status",
                        case_id=self.case.pk,
                        batch_id=batch.pk,
                    )
                )
                expect(
                    page.get_by_role("heading", name="Message analysis", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("Preparing message analysis", exact=True).first
                ).to_be_visible()
                self.capture(page, engine, "automatic-analysis-processing")
                self.db(prepare_case_analysis_batch, batch.pk)
                self.db(prepare_case_analysis_batch, batch.pk)
                batch = self.db(CaseAnalysisBatch.objects.get, pk=batch.pk)
                self.assertEqual(batch.status, "complete", batch.error_code)
                self.assertEqual(
                    self.db(batch.runs.filter(operation="entities").count), 1
                )
                self.assertEqual(
                    set(
                        self.db(
                            list,
                            batch.runs.filter(
                                parameters__has_key="backend"
                            ).values_list("parameters__backend", flat=True),
                        )
                    ),
                    {"gateway"},
                )
                page.get_by_role("link", name="Refresh status", exact=True).press(
                    "Enter"
                )
                expect(page.get_by_text("Checks queued", exact=True)).to_be_visible()
                expect(page.locator(".analysis-history-row")).to_have_count(
                    self.db(batch.runs.count)
                )
                page.goto(
                    self.url(
                        "phone:imessage_review", case_id=self.case.pk, run_id=final_id
                    )
                )
                page.get_by_role(
                    "link", name="Run message analysis again", exact=True
                ).click()
                expect(
                    page.get_by_role(
                        "heading", name="Analyze imported messages", exact=True
                    )
                ).to_be_visible()
                expect(both).to_be_checked()
                expect(approval).not_to_be_checked()
                ai.focus()
                expect(ai).to_be_focused()
                ai.press("Space")
                page.get_by_role(
                    "button", name="Run message analysis", exact=True
                ).click()
                expect(page.get_by_role("alert")).to_contain_text(
                    "Approve external processing to continue."
                )
                expect(ai).to_be_checked()
                expect(approval).not_to_be_checked()
                self.assertEqual(
                    self.db(
                        CaseAnalysisBatch.objects.filter(scope__id=str(final_id)).count
                    ),
                    1,
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
                    self.capture(page, engine, f"rerun-analysis-{width}-{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "rerun-analysis-dark-reduced-motion")
                page.emulate_media(forced_colors="active")
                approval.focus()
                expect(approval).to_be_focused()
                self.capture(page, engine, "rerun-analysis-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                approval.press("Space")
                expect(approval).to_be_checked()
                page.get_by_role(
                    "button", name="Run message analysis", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Message analysis", exact=True)
                ).to_be_visible()
                rerun = self.db(
                    CaseAnalysisBatch.objects.get,
                    scope__id=str(final_id),
                    scope__mode="ai",
                )
                self.assertNotEqual(rerun.pk, batch.pk)
                self.assertEqual(rerun.requested_by_id, self.owner.pk)
                self.assertTrue(rerun.scope["external_processing"])
                expect(page).to_have_url(
                    self.url(
                        "phone:imessage_analysis_status",
                        case_id=self.case.pk,
                        batch_id=rerun.pk,
                    )
                )
                expect(page.locator(".analysis-history-row")).to_have_count(0)
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
                    self.capture(page, engine, f"analysis-status-{width}-{height}")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 321
                )
                self.capture(page, engine, "analysis-status-large-text")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "analysis-status-dark")
                page.emulate_media(forced_colors="active")
                refresh = page.get_by_role("link", name="Refresh status", exact=True)
                refresh.focus()
                expect(refresh).to_be_focused()
                self.capture(page, engine, "analysis-status-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                with self.browser_page(engine) as (_, live_page):
                    self.sign_in(live_page)
                    status_url = self.url(
                        "phone:imessage_analysis_status",
                        case_id=self.case.pk,
                        batch_id=rerun.pk,
                    )
                    live_page.goto(status_url)
                    live_page.evaluate("window.statusSentinel = 42")
                    self.db(prepare_case_analysis_batch, rerun.pk)
                    self.db(prepare_case_analysis_batch, rerun.pk)
                    expect(live_page.locator(".analysis-history-row")).to_have_count(
                        self.db(rerun.runs.count), timeout=15000
                    )
                    region = live_page.locator("#message-analysis-status")
                    expect(region).to_have_attribute("data-live-poll", "true")
                    self.db(rerun.runs.update, status="cancelled")
                    expect(region).to_have_attribute(
                        "data-live-poll", "false", timeout=15000
                    )
                    self.assertEqual(live_page.evaluate("window.statusSentinel"), 42)
                    expect(live_page).to_have_url(status_url)
                    self.capture(live_page, engine, "analysis-status-finished")
                page.goto(
                    self.url(
                        "phone:imessage_review", case_id=self.case.pk, run_id=final_id
                    )
                )
                expect(
                    page.get_by_text("Selected analysis: AI only", exact=True)
                ).to_be_visible()
                self.capture(page, engine, "rerun-analysis-processing")

    @override_settings(
        PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY | {"account": ""},
        PANDECT_LOCAL_AI=SYNTHETIC_MESSAGE_LOCAL,
    )
    def test_ai_uses_local_service_without_external_approval_when_not_configured(self):
        for engine in self.engines:
            account = Person.objects.create(
                case=self.case, first_name=f"Account {engine}"
            )
            correspondent = Person.objects.create(
                case=self.case, first_name=f"Correspondent {engine}"
            )
            document = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title="Synthetic local AI import",
                file=SimpleUploadedFile("messages.csv", message_csv(), "text/csv"),
            )
            preview, _ = start_run(
                self.owner,
                self.case.pk,
                "imessage_import",
                [{"kind": "document", "id": str(document.pk)}],
                parameters={"phase": "preview", "timezone": "UTC"},
            )
            process_run(preview.pk)
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(
                    self.url(
                        "phone:imessage_review", case_id=self.case.pk, run_id=preview.pk
                    )
                )
                expect(
                    page.get_by_text(
                        "AI will use the configured private service.",
                        exact=True,
                    )
                ).to_be_visible()
                expect(page.locator("input[name='external_processing']")).to_have_count(
                    0
                )
                expect(
                    page.get_by_role("radio", name="Local checks", exact=True)
                ).to_be_checked()
                ai = page.get_by_role("radio", name="AI only", exact=True)
                ai.focus()
                expect(ai).to_be_focused()
                ai.press("Space")
                expect(ai).to_be_checked()
                account_fields = page.get_by_role(
                    "group", name="Export account — outgoing sender", exact=True
                )
                person = account_fields.get_by_label("Person", exact=True)
                person.select_option(label="Create a person")
                person_details = account_fields.get_by_text(
                    "New person details", exact=True
                )
                person_details.click()
                account_fields.get_by_label("Email", exact=True).fill(
                    "unfinished draft"
                )
                person.select_option(str(account.pk))
                person_details.click()
                page.get_by_role(
                    "group", name="Correspondent — incoming sender", exact=True
                ).get_by_label("Person", exact=True).select_option(
                    str(correspondent.pk)
                )
                page.get_by_label(
                    "I confirm this export is a direct conversation "
                    "between these two people",
                    exact=True,
                ).check()
                page.get_by_label(
                    "Import the ready messages with these people "
                    "and the confirmed timezone",
                    exact=True,
                ).check()
                for width, height in ((1440, 900), (390, 844), (320, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.locator("#import-analysis-heading").evaluate(
                        "element => element.scrollIntoView({block: 'start'})"
                    )
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"local-ai-review-{width}-{height}")
                page.get_by_role("button", name="Import messages", exact=True).click()
                page.wait_for_url(re.compile(r"/imessage/[0-9a-f-]+/(?:#.*)?$"))
                final_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                final = self.db(AnalysisRun.objects.get, pk=final_id)
                self.assertEqual(final.parameters["analysis"]["mode"], "ai")
                self.assertFalse(final.parameters["analysis"]["external_processing"])
                self.assertEqual(
                    {
                        route["backend"]
                        for route in final.parameters["analysis"]["executions"].values()
                    },
                    {"local"},
                )
                self.db(process_run, final_id)
                page.reload()
                expect(
                    page.get_by_role("heading", name="Messages imported", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("Selected analysis: AI only", exact=True)
                ).to_be_visible()
                batch = self.db(CaseAnalysisBatch.objects.get, scope__id=str(final_id))
                self.db(prepare_case_analysis_batch, batch.pk)
                self.db(prepare_case_analysis_batch, batch.pk)
                batch = self.db(CaseAnalysisBatch.objects.get, pk=batch.pk)
                self.assertEqual(batch.status, "complete", batch.error_code)
                self.assertFalse(
                    self.db(batch.runs.filter(operation="entities").exists)
                )
                self.assertFalse(
                    self.db(batch.runs.filter(external_processing=True).exists)
                )
                self.assertEqual(
                    set(
                        self.db(
                            list,
                            batch.runs.values_list("parameters__backend", flat=True),
                        )
                    ),
                    {"local"},
                )
                self.capture(page, engine, "local-ai-imported")

    def test_progress_completion_opens_prepared_review(self):
        existing = Person.objects.create(case=self.case, first_name="Existing person")
        for engine in self.engines:
            document = DocumentEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title="Synthetic live message review",
                file=SimpleUploadedFile("messages.csv", message_csv(), "text/csv"),
            )
            preview, _ = start_run(
                self.owner,
                self.case.pk,
                "imessage_import",
                [{"kind": "document", "id": str(document.pk)}],
                parameters={"phase": "preview", "timezone": "UTC"},
            )
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.sign_in(page)
                page.goto(
                    self.url(
                        "phone:imessage_review", case_id=self.case.pk, run_id=preview.pk
                    )
                )
                expect(
                    page.get_by_text("Preparing message review", exact=True)
                ).to_be_visible()
                self.capture(page, engine, "cheap-progress")
                self.db(process_run, preview.pk)
                ready = page.get_by_role(
                    "link", name="Review prepared messages", exact=True
                )
                expect(ready).to_be_visible(timeout=15000)
                expect(page.locator("#message-import")).to_have_attribute(
                    "data-live-poll", "false"
                )
                page.set_viewport_size({"width": 390, "height": 844})
                self.capture(page, engine, "cheap-progress-ready-mobile")
                ready.focus()
                expect(ready).to_be_focused()
                page.keyboard.press("Enter")
                expect(
                    page.get_by_role("button", name="Import messages", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("region", name="Message preview")
                ).to_contain_text("Original café")
                account = page.get_by_role(
                    "group", name="Export account — outgoing sender", exact=True
                )
                person = account.get_by_label("Person", exact=True)
                new_name = account.get_by_label("First name", exact=True)
                expect(new_name).to_be_hidden()
                person.select_option(label="Create a person")
                expect(new_name).to_be_visible()
                new_name.fill("Draft name")
                person.select_option(str(existing.pk))
                expect(new_name).to_be_hidden()
                person.select_option(label="Create a person")
                expect(new_name).to_have_value("Draft name")

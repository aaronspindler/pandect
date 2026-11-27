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
from core.analysis.services import start_run
from core.analysis.testing import external_grant_kwargs
from core.analysis.worker import process_run
from core.models import AnalysisRun, CaseAnalysisBatch
from document.models import DocumentEvidence
from people.models import Person
from phone.models import TextMessageEvidence
from phone.tests.test_imessage_views import (
    SYNTHETIC_MESSAGE_GATEWAY,
    message_csv,
    whatsapp_csv,
)


class IMessageImportBrowserTests(BrowserTestCase):
    artifact_subdirectory = "message-csv-browser"

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("imessage-browser-owner")
        self.case = Case.objects.create(owner=self.owner, name="Synthetic direct chat")

    def test_no_javascript_group_mapping_and_retained_message(self):
        long_identifier = "x" * 255
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("phone:imessage_upload", case_id=self.case.pk))
                page.get_by_label("Message CSV", exact=True).set_input_files(
                    {
                        "name": "synthetic-group.csv",
                        "mimeType": "text/csv",
                        "buffer": message_csv(
                            {
                                "Chat Session": "Synthetic group",
                                "Text": "First group body",
                            },
                            {
                                "Chat Session": "Synthetic group",
                                "Sender ID": long_identifier,
                                "Text": "Second group body",
                            },
                            {
                                "Chat Session": "Synthetic group",
                                "Type": "Outgoing",
                                "Sender ID": "",
                                "Sender Name": "",
                                "Text": "Group reply",
                            },
                            {
                                "Chat Session": "Synthetic group",
                                "Sender ID": "",
                                "Sender Name": "",
                                "Text": "Unattributed incoming message",
                            },
                        ),
                    }
                )
                page.get_by_label("Export timezone", exact=True).fill("UTC")
                page.get_by_label(
                    "I confirm this is the export's timezone", exact=True
                ).check()
                page.get_by_role(
                    "button", name="Prepare message review", exact=True
                ).click()
                page.wait_for_url(re.compile(r"/imessage/[0-9a-f-]+/$"))
                preview_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                self.db(process_run, preview_id)
                page.reload()
                expect(page.get_by_text("Group chat", exact=True)).to_be_visible()
                page.get_by_role("link", name="Review excluded rows").press("Enter")
                expect(
                    page.get_by_text("Incoming group message has no Sender ID.")
                ).to_be_visible()
                for group_name, name in (
                    ("Export account — outgoing sender", "Group account"),
                    ("Incoming sender 17055550100", "First group sender"),
                    (f"Incoming sender {long_identifier}", "Second group sender"),
                ):
                    sender = page.get_by_role("group", name=group_name, exact=True)
                    sender.get_by_label("Person", exact=True).select_option("create")
                    first_name = sender.get_by_label("First name", exact=True)
                    if not first_name.is_visible():
                        sender.get_by_text("New person details", exact=True).press(
                            "Enter"
                        )
                    first_name.fill(name)
                confirm = page.get_by_label(
                    "I confirm this export contains one group conversation", exact=True
                )
                confirm.focus()
                expect(confirm).to_be_focused()
                page.keyboard.press("Space")
                page.get_by_label(
                    "Import the ready messages with these people "
                    "and the confirmed timezone",
                    exact=True,
                ).check()
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
                    self.capture(page, engine, f"group-review-{width}-{height}")
                page.set_viewport_size({"width": 390, "height": 844})
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self.assertLessEqual(
                    page.evaluate("document.documentElement.scrollWidth"), 391
                )
                self.capture(page, engine, "group-review-enlarged-text")
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "group-review-dark-reduced-motion")
                page.emulate_media(forced_colors="active")
                confirm.focus()
                expect(confirm).to_be_focused()
                self.capture(page, engine, "group-review-forced-colors")
                page.emulate_media(color_scheme="light", forced_colors="none")
                page.get_by_role("button", name="Import messages", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role("heading", name="Importing messages", exact=True)
                ).to_be_visible()
                final_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                self.assertNotEqual(preview_id, final_id)
                self.db(process_run, final_id)
                page.reload()
                expect(
                    page.get_by_role("heading", name="Messages imported", exact=True)
                ).to_be_visible()
                expect(page.get_by_text("Retained messages · 3")).to_be_visible()
                expect(
                    page.get_by_role("link", name="View conversation", exact=True)
                ).to_have_count(0)
                self.capture(page, engine, "group-imported-mobile")
                page.get_by_role("link", name="View retained message").press("Enter")
                reader = page.get_by_role("region", name="Selected evidence")
                expect(
                    reader.get_by_text("First group body", exact=True)
                ).to_be_visible()
                expect(
                    reader.get_by_role("link", name="View retained CSV")
                ).to_be_visible()

    @override_settings(
        PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY,
    )
    def test_no_javascript_upload_review_people_and_import(self):
        self.db(external_grant_kwargs, self.case, "content_events")
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("phone:imessage_upload", case_id=self.case.pk))
                page.get_by_label("Message CSV", exact=True).set_input_files(
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
                expect(page.locator("input[name='external_processing']")).to_have_count(
                    0
                )
                expect(page.locator("input[name='mode']")).to_have_count(0)
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
                expect(
                    page.get_by_role("heading", name="Importing messages", exact=True)
                ).to_be_visible()
                final_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                self.assertEqual(self.db(Person.objects.count), before)
                self.db(process_run, final_id)
                page.reload()
                expect(
                    page.get_by_role("heading", name="Messages imported", exact=True)
                ).to_be_visible()
                final = self.db(AnalysisRun.objects.get, pk=final_id)
                self.assertEqual(final.status, "complete", final.error_code)
                self.assertNotIn("analysis", final.parameters)
                batch = self.db(CaseAnalysisBatch.objects.get, scope__id=str(final_id))
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
                expect(page.locator("#message-analysis-status")).to_contain_text(
                    "0 of 2 imported messages prepared · 0 checks created"
                )
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
                rerun_action = page.get_by_role(
                    "button", name="Run import analysis again", exact=True
                )
                rerun_action.focus()
                expect(rerun_action).to_be_focused()
                rerun_action.press("Enter")
                expect(
                    page.get_by_role("heading", name="Message analysis", exact=True)
                ).to_be_visible()
                rerun = self.db(
                    CaseAnalysisBatch.objects.exclude(pk=batch.pk).get,
                    scope__id=str(final_id),
                )
                self.assertEqual(rerun.requested_by_id, self.owner.pk)
                self.assertEqual(rerun.scope["executions"], batch.scope["executions"])
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
                    page.get_by_role("link", name="Follow message analysis", exact=True)
                ).to_have_attribute("href", re.compile(str(rerun.pk)))
                self.capture(page, engine, "rerun-analysis-processing")

    def test_no_javascript_whatsapp_import_opens_conversations_with_source(self):
        for engine in self.engines:
            account = Person.objects.create(
                case=self.case, first_name=f"Avery {engine}"
            )
            correspondent = Person.objects.create(
                case=self.case, first_name=f"Blair {engine}"
            )
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:evidence:bulk_create", case_id=self.case.pk))
                page.get_by_role("link", name="Import message CSV", exact=True).click()
                page.get_by_label("Message CSV", exact=True).set_input_files(
                    {
                        "name": "synthetic-whatsapp.csv",
                        "mimeType": "text/csv",
                        "buffer": whatsapp_csv(),
                    }
                )
                page.get_by_label("Export timezone", exact=True).fill("America/Toronto")
                page.get_by_label("I confirm this is the export's timezone").check()
                page.get_by_role("button", name="Prepare message review").click()
                preview_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                self.db(process_run, preview_id)
                page.reload()
                expect(
                    page.get_by_role("heading", name="Review message CSV")
                ).to_be_visible()
                expect(page.get_by_text("WhatsApp CSV", exact=True)).to_be_visible()
                self.capture(page, engine, "whatsapp-review-1440-900")
                page.set_viewport_size({"width": 390, "height": 844})
                expect(page.get_by_text("WhatsApp CSV", exact=True)).to_be_visible()
                self.capture(page, engine, "whatsapp-review-390-844")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.get_by_role(
                    "group", name="Export account — outgoing sender", exact=True
                ).get_by_label("Person", exact=True).select_option(str(account.pk))
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
                page.get_by_role("button", name="Import messages", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Importing messages", exact=True)
                ).to_be_visible()
                final_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                self.db(process_run, final_id)
                page.reload()
                expect(
                    page.get_by_role("heading", name="Messages imported")
                ).to_be_visible()
                page.get_by_role("link", name="View conversation", exact=True).click()
                messages = page.get_by_role("region", name="Messages", exact=True)
                expect(
                    messages.get_by_text("Original WhatsApp message, café", exact=False)
                ).to_be_visible()
                expect(
                    messages.get_by_text("WhatsApp reply", exact=True)
                ).to_be_visible()
                expect(
                    messages.get_by_text("Messages: synthetic-whatsapp.csv · Row 2")
                ).to_be_visible()
                for width, height in ((1440, 900), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(
                        page, engine, f"whatsapp-conversation-{width}-{height}"
                    )
                messages.locator("[data-message-select]").first.click()
                page.get_by_text("Message source", exact=True).click()
                page.get_by_role("link", name="View source metadata").click()
                info = self.open_evidence_panel(page, "info")
                expect(
                    info.get_by_role("heading", name="Imported source context")
                ).to_be_visible()
                info.get_by_role(
                    "link", name="Retained export: Messages: synthetic-whatsapp.csv"
                ).click()
                expect(
                    page.get_by_role(
                        "heading", name="Messages: synthetic-whatsapp.csv", exact=True
                    )
                ).to_be_visible()

    @override_settings(
        PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY | {"account": ""},
    )
    def test_deterministic_processing_continues_with_external_ai_disabled(self):
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
                title="Synthetic import with external AI disabled",
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
                expect(page.locator("input[name='external_processing']")).to_have_count(
                    0
                )
                expect(page.locator("input[name='mode']")).to_have_count(0)
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
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width + 1
                    )
                    self.capture(page, engine, f"disabled-ai-review-{width}-{height}")
                page.get_by_role("button", name="Import messages", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Importing messages", exact=True)
                ).to_be_visible()
                final_id = UUID(urlsplit(page.url).path.rstrip("/").split("/")[-1])
                final = self.db(AnalysisRun.objects.get, pk=final_id)
                self.assertNotIn("analysis", final.parameters)
                self.db(process_run, final_id)
                page.reload()
                expect(
                    page.get_by_role("heading", name="Messages imported", exact=True)
                ).to_be_visible()
                batch = self.db(CaseAnalysisBatch.objects.get, scope__id=str(final_id))
                self.db(prepare_case_analysis_batch, batch.pk)
                self.db(prepare_case_analysis_batch, batch.pk)
                batch = self.db(CaseAnalysisBatch.objects.get, pk=batch.pk)
                self.assertEqual(batch.status, "complete", batch.error_code)
                self.assertTrue(self.db(batch.runs.filter(operation="entities").exists))
                self.assertFalse(
                    self.db(batch.runs.filter(external_processing=True).exists)
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
                    set(),
                )
                self.capture(page, engine, "disabled-ai-imported")

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
                expect(page.locator("[data-live-message]")).to_have_text(
                    "Message review ready"
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

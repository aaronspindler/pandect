"""Synthetic journeys: run explicitly with manage.py test core.analysis.browser."""

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image
from playwright.sync_api import expect

from audio.models import AudioEvidence
from cases.models import Case, CaseMembership
from config.tests.browser_base import BrowserTestCase
from core.analysis.approval import revoke_case_execution
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.catalogue import project_result
from core.analysis.navigation import finding_url
from core.analysis.services import (
    cancel_run,
    process_run,
    resolve_inputs,
    snapshot_sources,
    start_run,
)
from core.analysis.testing import external_grant_kwargs, wav_bytes
from core.analysis.tests.test_openai_gateway import AUTO_OPENAI, OPENAI
from core.analysis.tests.test_topic_recovery import LOCAL
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch
from document.models import DocumentEvidence
from geo.analysis.external.geocoding import Geocoder
from geo.models import LocationEvidence, PersonLocationEvidence, VehicleLocationEvidence
from geo.testing import LOCAL_GEOCODER
from image.models import ImageEvidence
from people.models import Person
from phone.analysis.ingest import describe_import_selection
from phone.models import (
    CallEvidenceAttachment,
    CommunicationContext,
    PhoneCallEvidence,
    TextMessageEvidence,
)
from text.analysis.ai import content_events
from text.models import TextEvidence
from vehicle.models import Vehicle

REVIEWED_OPENAI = AUTO_OPENAI | {"workers_token": "synthetic-workers-token"}


class AnalysisBrowserTests(BrowserTestCase):
    artifact_subdirectory = "analysis-browser"

    @override_settings(
        PANDECT_LOCAL_AI=LOCAL,
        PANDECT_AI_GATEWAY=OPENAI | {"text_provider": "anthropic-direct"},
        PANDECT_DIRECT_AI={
            "anthropic_api_key": "synthetic-claude-key",
            "anthropic_model": "claude-haiku-5-5",
        },
    )
    def test_direct_ai_review_and_queue_without_javascript(self):
        settings_url = self.url("cases:case_settings", case_id=self.case.pk)
        analysis_url = (
            self.url("core:analysis:create", case_id=self.case.pk)
            + "?operation=content_events"
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.owner)
                page.goto(settings_url)
                ai = page.locator("#ai-processing")
                expect(ai.get_by_role("radio", name="Local AI")).to_be_checked()
                ai.get_by_role("radio", name="External AI").press("Space")
                ai.get_by_role("button", name="Save changes").press("Enter")
                expect(
                    page.get_by_role("heading", name="Review external AI destinations")
                ).to_be_visible()
                expect(page.get_by_role("checkbox", name="Anthropic")).to_be_checked()
                expect(
                    page.get_by_text("Direct connection", exact=False)
                ).to_be_visible()
                self.assertNotIn("synthetic-claude-key", page.content())
                page.get_by_role("button", name="Approve and save").press("Enter")
                expect(ai.get_by_role("radio", name="External AI")).to_be_checked()
                page.goto(analysis_url)
                expect(
                    page.get_by_label("Processing location", exact=True)
                ).to_have_count(0)
                expect(page.locator('input[name="external_processing"]')).to_have_count(
                    0
                )
                page.locator(
                    f'input[name="sources"][value="text:{self.text.pk}"]'
                ).check()
                page.get_by_role("button", name="Start analysis", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role(
                        "heading", name="Suggest claims and commitments", exact=True
                    )
                ).to_be_visible()
                batch = self.db(
                    lambda: CaseAnalysisBatch.objects.filter(case=self.case).latest(
                        "created_at"
                    )
                )
                self.assertEqual(
                    self.db(
                        lambda batch=batch: list(
                            batch.runs.values_list("parameters__backend", flat=True)
                        )
                    ),
                    [Case.AIBackend.GATEWAY],
                )
                self.assertTrue(
                    self.db(
                        batch.runs.filter(
                            external_processing=True,
                            parameters___execution__provider="anthropic",
                        ).exists
                    )
                )
                page.goto(settings_url)
                ai.locator("summary").press("Enter")
                ai.get_by_role(
                    "button", name="Revoke external analysis approval"
                ).press("Enter")
                expect(ai.locator("summary")).to_contain_text("None approved")
                ai.get_by_role("radio", name="Local AI").press("Space")
                ai.get_by_role("button", name="Save changes").press("Enter")
                expect(ai.get_by_role("radio", name="Local AI")).to_be_checked()

    @override_settings(
        PANDECT_AI_GATEWAY=REVIEWED_OPENAI | {"unified_billing": True},
        PANDECT_LOCAL_AI=LOCAL,
        PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway",
    )
    def test_openai_models_and_destination_review_without_javascript(self):
        settings_url = self.url("cases:case_settings", case_id=self.case.pk)
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.db(
                    Case.objects.filter(pk=self.case.pk).update,
                    ai_backend=Case.AIBackend.LOCAL,
                    external_analysis_models={},
                    external_analysis_approval={},
                    search_embedding_approval={},
                )
                self.sign_in(page, self.owner)
                page.goto(settings_url)
                ai = page.locator("#ai-processing")
                save = ai.get_by_role("button", name="Save changes")
                external = ai.get_by_role("radio", name="External AI")
                external.press("Space")
                save.press("Enter")
                approve = page.get_by_role("button", name="Approve and save")
                expect(approve).to_be_visible()
                expect(page.get_by_text("Audio chunks", exact=False)).to_be_visible()
                page.get_by_role("link", name="Cancel", exact=True).click()
                expect(ai.get_by_role("radio", name="Local AI")).to_be_checked()
                external.press("Space")
                save.press("Enter")
                page.get_by_role("checkbox", name="Cloudflare Workers AI").uncheck()
                approve.press("Enter")
                expect(external).to_be_checked()
                save.press("Enter")
                expect(
                    page.get_by_text("AI processing settings saved.")
                ).to_be_visible()
                expect(approve).to_have_count(0)

                claims_model = ai.get_by_label("Claims and commitments", exact=True)
                claims_model.select_option("gpt-6.2-sol")
                save.press("Enter")
                expect(
                    page.get_by_text("AI processing settings saved.")
                ).to_be_visible()
                expect(claims_model).to_have_value("gpt-6.2-sol")
                ai.locator("summary").press("Enter")
                ai.get_by_role("button", name="Review pending destinations").press(
                    "Enter"
                )
                expect(page.get_by_role("checkbox")).to_have_count(1)
                expect(page.get_by_text("Audio chunks", exact=False)).to_be_visible()
                approve.press("Enter")

                chat_model = ai.get_by_label("Evidence chat", exact=True)
                chat_model.select_option("anthropic/claude-sonnet-5.5")
                save.press("Enter")
                expect(page.get_by_role("checkbox")).to_have_count(1)
                expect(page.get_by_text("Evidence chat.", exact=False)).to_be_visible()
                approve.press("Enter")
                expect(chat_model).to_have_value("anthropic/claude-sonnet-5.5")
                chat_model.select_option("gpt-6.2-sol")
                save.press("Enter")
                expect(chat_model).to_have_value("gpt-6.2-sol")
                expect(approve).to_have_count(0)
                page.get_by_role("button", name="Approve and start indexing").press(
                    "Enter"
                )
                page.wait_for_url("**/processing/#case-search-index")
                page.goto(settings_url)
                expect(
                    page.get_by_role("link", name="View indexing progress")
                ).to_be_visible()

    @override_settings(
        PANDECT_AI_GATEWAY=REVIEWED_OPENAI | {"unified_billing": True},
        PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway",
    )
    def test_searchable_model_picker_and_settings_layout(self):
        settings_url = self.url("cases:case_settings", case_id=self.case.pk)
        for engine in self.engines:
            with self.subTest(engine=engine), self.browser_page(engine) as (_, page):
                self.db(
                    Case.objects.filter(pk=self.case.pk).update,
                    ai_backend=Case.AIBackend.LOCAL,
                    external_analysis_models={
                        "question_synthesis": "google/gemini-4-argon"
                    },
                    external_analysis_approval={},
                    search_embedding_approval={},
                )
                self.sign_in(page, self.owner)
                page.goto(settings_url)
                ai = page.locator("#ai-processing")
                chat = ai.get_by_role("button", name=re.compile("^Evidence chat:"))
                chat.press("Enter")
                picker = page.get_by_role("dialog", name="Model for Evidence chat")
                search = picker.get_by_label("Search models or providers")
                models = picker.get_by_label("Matching models")
                expect(search).to_be_focused()
                search.fill("no such model")
                expect(picker.get_by_text("No matching models.")).to_be_visible()
                expect(models.locator("optgroup")).to_have_count(0)
                expect(picker.get_by_role("button", name="Use model")).to_be_disabled()
                search.press("Escape")
                # Search inputs can consume the first Escape to clear their text.
                if picker.is_visible():
                    search.press("Escape")
                expect(chat).to_be_focused()
                chat.press("Enter")
                search.fill("Vertex")
                expect(
                    models.locator('option[value="google/gemini-4-argon"]')
                ).to_have_count(1)
                search.fill("anthropic")
                models.select_option("anthropic/claude-sonnet-5.5")
                self.capture(page, engine, "ai-model-picker")
                picker.get_by_role("button", name="Use model").press("Enter")
                expect(chat).to_contain_text("Claude Sonnet 5.5")
                expect(chat).to_be_focused()
                ai.get_by_role("radio", name="External AI").press("Space")
                ai.get_by_role("button", name="Save changes").press("Enter")
                self.capture(page, engine, "ai-destination-review")
                page.get_by_role("button", name="Approve and save").press("Enter")
                expect(chat).to_contain_text("Claude Sonnet 5.5")
                page.get_by_role("button", name="Approve and start indexing").press(
                    "Enter"
                )
                page.wait_for_url("**/processing/#case-search-index")
                page.goto(settings_url + "#ai-processing")
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    for theme in ("light", "dark"):
                        page.emulate_media(color_scheme=theme)
                        ai.evaluate("section => section.scrollIntoView()")
                        self._assert_reflow(page)
                        self.capture(
                            page, engine, f"ai-settings-{theme}-{width}-{height}"
                        )
                page.emulate_media(forced_colors="active", reduced_motion="reduce")
                ai.get_by_role("radio", name="External AI").focus()
                expect(ai.get_by_role("radio", name="External AI")).to_be_focused()
                self.capture(page, engine, "ai-settings-forced-colors")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self._assert_reflow(page)

    def setUp(self):
        super().setUp()
        self.owner = self.make_user("analysis-browser-owner")
        self.viewer = self.make_user("analysis-browser-viewer")
        self.case = Case.objects.create(
            owner=self.owner, name="Synthetic analysis case"
        )
        CaseMembership.objects.create(case=self.case, user=self.viewer, role="viewer")
        self.document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic retained report",
            file=ContentFile(
                b"Synthetic source text.\n"
                b"Contact source@analysis.test for the proposed meeting.\n"
                b"<iframe>source wording</iframe>\n",
                name="report.txt",
            ),
        )
        self.text = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic captured notes",
            body="A proposed meeting remains unconfirmed. Contact note@analysis.test.",
        )
        person = Person.objects.create(
            case=self.case, first_name="Synthetic participant"
        )
        PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic metadata-only call",
            sender=person,
            recipient=person,
        )

    def _complete(self, run_id):
        process_run(run_id)
        run = AnalysisRun.objects.get(pk=run_id)
        if run.status != "complete":
            raise AssertionError(f"Synthetic extraction failed: {run.error_code}")

    def _revoke_viewer(self):
        CaseMembership.objects.filter(case=self.case, user=self.viewer).delete()

    def _assert_reflow(self, page):
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"),
            page.evaluate("document.documentElement.clientWidth") + 1,
        )

    def test_focused_imported_correction_separates_suggestion_and_quotation(self):
        participant = Person.objects.get(case=self.case)
        quote = "I was wrong. The delivery is Friday, not Thursday."
        prefix = "About the delivery: "
        suggestion = "The sender corrected the delivery date to Friday."
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Delivery correspondence",
            sender=participant,
            recipient=participant,
            body=prefix + quote + " Thank you.",
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=message,
            source_document=self.document,
            source_sha256=self.document.sha256,
            source_row=7,
            source_start=0,
            source_end=1,
            parser_version="synthetic.v1",
            timezone="UTC",
            raw_sender="Synthetic correspondent",
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="content_events",
            method_version="test",
            inputs=snapshot_sources(
                self.case, [{"kind": "text-message", "id": message.pk}]
            ),
            parameters={
                "_import_analysis": {
                    "selected_messages": 1,
                    "imported_messages": 1,
                    "additional_calls": 0,
                    "matching_calls": 0,
                }
            },
            status=AnalysisRun.Status.COMPLETE,
        )
        sources = resolve_inputs(run, self.case)
        data = content_events(
            sources,
            {},
            infer=lambda *_: {
                "items": [
                    {
                        "kind": "correction",
                        "text": suggestion,
                        "status": "supported",
                        "citations": [{"source_id": sources[0].id, "quote": quote}],
                    }
                ]
            },
        )
        describe_import_selection(data, run)
        result = AnalysisResult.objects.create(case=self.case, run=run, data=data)
        project_result(result)
        basis = data["items"][0]["values"]["basis"]
        legacy_warning = "This earlier import lacks delivery confirmation."
        legacy_status = "requires manual reconciliation"
        legacy_run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation=run.operation,
            method_version="retained-test",
            inputs=run.inputs,
            status=AnalysisRun.Status.COMPLETE,
        )
        AnalysisResult.objects.create(
            case=self.case,
            run=legacy_run,
            data={
                "items": [data["items"][0] | {"values": {"status": legacy_status}}],
                "warnings": [legacy_warning],
            },
        )
        detail = self.live_server_url + finding_url(
            self.case.pk, run.pk, 0, history={"return_findings": ""}
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(detail)
                finding = page.locator("#finding-1")
                expect(
                    page.get_by_role("link", name="Back to findings")
                ).to_be_visible()
                expect(
                    finding.get_by_role("heading", name="AI suggestion · Correction")
                ).to_be_visible()
                expect(
                    finding.get_by_role("heading", name="Cited evidence")
                ).to_be_visible()
                qualification = finding.get_by_text(
                    "The cited wording matches the retained source.", exact=False
                )
                expect(qualification).to_be_visible()
                excerpt = finding.locator("blockquote.analysis-source-excerpt")
                expect(excerpt).to_have_count(1)
                self.assertEqual(excerpt.text_content().strip(), quote)
                content = finding.inner_text()
                self.assertLess(content.index(suggestion), content.index(quote))
                self.assertEqual(content.count(quote), 1)
                expect(
                    finding.get_by_role(
                        "link", name="Imported message · Export row 7", exact=True
                    )
                ).to_have_attribute("href", sources[0].original_url)
                expect(
                    finding.get_by_text(
                        "Selected 1 of 1 newly imported messages", exact=False
                    )
                ).to_be_visible()
                details = finding.locator(".analysis-finding-details")
                details_summary = details.get_by_text("Finding details", exact=True)
                saved_status = details.get_by_text("supported", exact=True)
                exact_offset = details.get_by_text(
                    f"Characters {len(prefix)}–{len(prefix + quote)}", exact=True
                )
                expect(details).not_to_have_attribute("open", "")
                expect(finding.get_by_text("supported", exact=True)).to_be_hidden()
                expect(exact_offset).to_be_hidden()
                details_summary.press("Enter")
                expect(saved_status).to_be_visible()
                expect(details.get_by_text(basis, exact=True)).to_be_visible()
                expect(exact_offset).to_be_visible()
                details_summary.press("Enter")

                run_details = page.locator(".analysis-run-details")
                expect(run_details).not_to_have_attribute("open", "")
                original_warning = run_details.get_by_text(
                    data["warnings"][-1], exact=True
                )
                expect(original_warning).to_be_hidden()
                run_summary = run_details.get_by_text(
                    "Analysis run details", exact=True
                )
                run_summary.press("Enter")
                expect(original_warning).to_be_visible()
                run_summary.press("Enter")

                for width, height in ((1440, 900), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("document.activeElement.blur()")
                    page.evaluate("window.scrollTo(0, 0)")
                    self._assert_reflow(page)
                    self.capture(page, engine, f"imported-correction-{width}")

                page.goto(
                    self.url(
                        "core:analysis:detail",
                        case_id=self.case.pk,
                        run_id=legacy_run.pk,
                    )
                )
                expect(finding.get_by_text(legacy_status, exact=True)).to_be_visible()
                expect(finding.get_by_text(legacy_warning, exact=True)).to_be_visible()
                expect(qualification).to_have_count(0)
                expect(excerpt).to_be_visible()

    def test_streamlined_timing_finding_reveal_and_source_details(self):
        self.owner.timezone = "America/Toronto"
        self.owner.save(update_fields=["timezone"])
        sender = Person.objects.create(case=self.case, first_name="Alex")
        recipient = Person.objects.create(case=self.case, first_name="Morgan")
        messages = []
        for index, body in enumerate(("Tomorrow will be better.", "Thank you!")):
            message = TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Imported message · row {index + 2}",
                sender=sender if index == 0 else recipient,
                recipient=recipient if index == 0 else sender,
                occurred_at=datetime(2026, 7, 30, 1, 23, 10, tzinfo=UTC)
                + timedelta(seconds=16 * index),
                body=body,
            )
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=self.document,
                source_sha256=self.document.sha256,
                source_row=index + 2,
                source_start=index,
                source_end=index + 1,
                parser_version="synthetic.v1",
                timezone="UTC",
                raw_sender="Synthetic correspondent",
                native_message_id=f"synthetic-native-{index}",
            )
            messages.append(message)
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "conversation_link",
            [{"kind": "text-message", "id": str(item.pk)} for item in messages],
        )
        self._complete(run.pk)
        url = self.url("core:analysis:detail", case_id=self.case.pk, run_id=run.pk)
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(url)
                expect(page.locator(".analysis-finding")).to_have_count(0)
                expect(
                    page.get_by_text("1 timing-only finding hidden.")
                ).to_be_visible()
                show = page.get_by_role("link", name="Show all findings", exact=True)
                show.focus()
                show.press("Enter")
                finding = page.locator(".analysis-finding")
                expect(
                    finding.get_by_role("heading", name="Messages 16 seconds apart")
                ).to_be_visible()
                for message in messages:
                    expect(finding.get_by_text(message.body, exact=True)).to_have_count(
                        1
                    )
                expect(finding.locator("time").first).to_contain_text(
                    "29 Jul 2026, 21:23:10 EDT"
                )
                details = finding.locator(".analysis-finding-details")
                expect(details).not_to_have_attribute("open", "")
                native_id = finding.get_by_text("synthetic-native-0", exact=True)
                expect(native_id).to_be_hidden()
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"streamlined-finding-{width}-{height}")
                summary = details.locator(":scope > summary")
                summary.focus()
                summary.press("Enter")
                expect(native_id).to_be_visible()
                summary.press("Enter")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self._assert_reflow(page)
                page.evaluate("document.documentElement.style.fontSize = ''")
                page.evaluate("document.documentElement.style.zoom = '4'")
                self._assert_reflow(page)
                page.evaluate("document.documentElement.style.zoom = ''")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                page.evaluate("window.scrollTo(0, 0)")
                self.capture(page, engine, "streamlined-finding-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "streamlined-finding-forced-colors")
                page.get_by_role("link", name="Hide timing-only findings").click()
                expect(page.locator(".analysis-finding")).to_have_count(0)
                page.goto(url + "?finding=1")
                expect(page.locator(".analysis-finding")).to_be_visible()

    def _many_findings(self):
        source = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title=self.text.title,
            body="\n".join(
                f"contact-{number:02}@example.test" for number in range(1, 61)
            ),
        )
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "entities",
            [{"kind": "text", "id": str(source.pk)}],
        )
        self._complete(run.pk)
        return run.pk

    def test_message_analysis_sources_and_keyboard_flow_without_javascript(self):
        person = Person.objects.get(case=self.case)
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic message for session analysis",
            sender=person,
            recipient=person,
            occurred_at=datetime(2026, 11, 15, 12, tzinfo=UTC),
            body="The proposed meeting is at noon.",
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                chooser = self.url("core:analysis:create", case_id=self.case.pk)
                for label in (
                    "Read inferred message sessions",
                    "Inspect message timing",
                    "Summarize message sessions",
                ):
                    page.goto(chooser)
                    expect(
                        page.get_by_role(
                            "heading", name="Inspect communications", exact=True
                        )
                    ).to_be_visible()
                    link = page.get_by_role("link", name=label, exact=True)
                    link.focus()
                    expect(link).to_be_focused()
                    link.press("Enter")
                    expect(page.get_by_role("heading", name=label)).to_be_visible()
                    choices = page.locator('input[name="sources"]')
                    expect(choices).to_have_count(1)
                    expect(choices).to_have_value(f"text-message:{message.pk}")
                    expect(
                        page.get_by_text("Synthetic metadata-only call")
                    ).to_have_count(0)
                    expect(page.get_by_text(self.text.title)).to_have_count(0)
                    guidance = page.locator(".disclosure").filter(
                        has_text="Supported sources"
                    )
                    guidance.locator("summary").press("Enter")
                    expect(guidance.locator("p")).to_be_visible()

                for width, height, label in (
                    (1440, 900, "desktop"),
                    (1440, 1024, "tall-desktop"),
                    (768, 1024, "tablet"),
                    (390, 844, "mobile"),
                    (320, 844, "reflow"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"message-analysis-{label}")
                    if label in {"desktop", "mobile", "reflow"}:
                        page.screenshot(
                            path=str(
                                self.artifacts
                                / f"{engine}-message-guidance-{label}.png"
                            ),
                            full_page=True,
                        )
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self._assert_reflow(page)
                self.capture(page, engine, "message-analysis-large-text")
                page.reload()
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "message-analysis-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "message-analysis-forced-colors")
                page.emulate_media(forced_colors="none")

                page.goto(chooser + "?operation=sessions")
                source = page.get_by_label(
                    f"Text message · {message.title}", exact=True
                )
                source.focus()
                expect(source).to_be_focused()
                self.assertNotEqual(
                    source.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                source.press("Space")
                button = page.get_by_role("button", name="Start analysis", exact=True)
                button.focus()
                button.press("Enter")
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
                ).to_be_visible()
                run_id = UUID(page.url.rstrip("/").split("/")[-1])
                self.db(self._complete, run_id)
                page.get_by_role("link", name="Refresh status", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Inferred session 1", exact=True)
                ).to_be_visible()
                expect(page.get_by_text(message.body, exact=True)).to_be_visible()

    def test_reprocess_case_evidence_with_keyboard_without_javascript(self):
        def finish_preparation():
            batch = CaseAnalysisBatch.objects.get(case=self.case)
            for _ in range(20):
                prepare_case_analysis_batch(str(batch.pk))
                batch.refresh_from_db()
                if batch.status != "queued":
                    break
            self.assertEqual(batch.status, "complete")
            self.assertGreaterEqual(batch.queued_runs, 3)
            return batch

        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.db(CaseAnalysisBatch.objects.filter(case=self.case).delete)
                self.sign_in(page)
                index_url = self.url("core:analysis:processing", case_id=self.case.pk)
                page.goto(index_url)
                actions = page.locator("#processing-actions > summary")
                actions.press("Enter")
                link = page.get_by_role("link", name="Reprocess evidence", exact=True)
                expect(link).to_be_visible()
                expect(
                    page.get_by_role("button", name="Run local checks")
                ).to_have_count(0)
                link.focus()
                expect(link).to_be_focused()
                link.press("Enter")
                button = page.get_by_role(
                    "button", name="Start reprocessing", exact=True
                )
                for width, height, label in (
                    (1440, 900, "desktop"),
                    (1440, 1024, "tall-desktop"),
                    (768, 1024, "tablet"),
                    (390, 844, "mobile"),
                    (320, 844, "reflow"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    expect(button).to_be_visible()
                    self.assertGreaterEqual(button.bounding_box()["height"], 44)
                    self.capture(page, engine, f"rerun-all-{label}")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self._assert_reflow(page)
                self.capture(page, engine, "rerun-all-large-text")
                page.reload()
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "rerun-all-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "rerun-all-forced-colors")
                page.emulate_media(forced_colors="none")
                button.focus()
                expect(button).to_be_focused()
                button.press("Enter")
                expect(
                    page.get_by_role(
                        "link", name="Preparing selected checks", exact=False
                    )
                ).to_be_visible()
                self.capture(page, engine, "rerun-all-preparing")
                batch = self.db(finish_preparation)
                page.goto(index_url + f"?batch={batch.pk}")
                expect(page.locator("#processing-history .notice")).to_contain_text(
                    "Selected batch · Checks queued"
                )
                expect(page.locator("#processing-history .notice")).to_contain_text(
                    f"{batch.processed_sources} sources prepared · "
                    f"{batch.queued_runs} initial checks queued"
                )
                self.capture(page, engine, "rerun-all-queued")
            with self.browser_page(engine, java_script_enabled=False) as (_, page):
                self.sign_in(page, self.viewer)
                page.goto(index_url)
                expect(
                    page.get_by_role("link", name="Reprocess evidence", exact=True)
                ).to_have_count(0)

    def test_analysis_overview_reports_and_responsive_layout(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.db(AnalysisRun.objects.filter(case=self.case).delete)
                self.sign_in(page)
                index_url = self.url("core:analysis:index", case_id=self.case.pk)
                page.goto(index_url)
                latest = page.get_by_role("region", name="Latest records")
                expect(latest.locator("time")).to_have_count(3)
                expect(latest.get_by_text("Finding", exact=True)).to_have_count(0)
                self.capture(page, engine, "case-tools-evidence-only")
                self.db(self._many_findings)
                page.goto(index_url)
                expect(latest.locator("time")).to_have_count(6)
                expect(
                    latest.get_by_role("link", name="contact-01@example.test")
                ).to_be_visible()
                expect(
                    page.get_by_role("heading", name="Case Tools", exact=True)
                ).to_be_visible()
                expect(page.locator("#processing-history")).to_have_count(0)
                expect(
                    page.get_by_role("button", name="Run local checks", exact=True)
                ).to_have_count(0)
                directory = page.get_by_role("navigation", name="Explore case")
                reports = directory.get_by_role("region", name="Reports")
                expect(reports.get_by_role("link")).to_have_count(4)
                for label in (
                    "Findings",
                    "Conversations",
                    "Trips",
                    "Images",
                    "Important",
                    "Chronology",
                    "People",
                    "Vehicles",
                    "Repeated identifiers",
                    "Sighting groups",
                ):
                    expect(
                        directory.get_by_role("link", name=label, exact=True)
                    ).to_be_visible()
                navigation = page.get_by_role("navigation", name="Investigation")
                navigation.get_by_role("link", name="Processing", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_role("heading", name="Processing", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name="Browse findings", exact=True)
                ).to_be_visible()
                history = page.locator("#processing-history")
                expect(history).not_to_have_attribute("open", "")
                history.locator(":scope > summary").press("Enter")
                page.locator("#processing-history summary").filter(
                    has_text="Filter history"
                ).press("Enter")
                page.get_by_label("Status", exact=True).select_option("cancelled")
                page.get_by_role("button", name="Filter history", exact=True).press(
                    "Enter"
                )
                expect(
                    page.get_by_text("No checks match these filters.", exact=True)
                ).to_be_visible()
                page.get_by_role("link", name="Clear filters", exact=True).press(
                    "Enter"
                )
                row = history.locator(".analysis-history-row").first
                expect(row.locator("strong")).to_have_text(self.text.title)
                expect(row.get_by_text("Saved result", exact=True)).to_be_visible()
                row.press("Enter")
                expect(
                    page.get_by_text("contact-01@example.test").first
                ).to_be_visible()
                evidence = page.get_by_role(
                    "link", name="All findings for this evidence"
                )
                expect(evidence).to_be_visible()
                evidence.press("Enter")
                expect(page).to_have_url(re.compile(r"#evidence-findings-heading$"))
                navigation.get_by_role("link", name="Case Tools", exact=True).press(
                    "Enter"
                )
                methods = page.get_by_role("region", name="Analysis methods")
                tool_links = directory.get_by_role("link").or_(
                    methods.get_by_role("link")
                )
                for width, height, label in (
                    (1440, 900, "desktop"),
                    (1440, 1024, "tall-desktop"),
                    (768, 1024, "tablet"),
                    (390, 844, "mobile"),
                    (320, 844, "reflow"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    for link in tool_links.all():
                        bounds = link.bounding_box()
                        self.assertIsNotNone(bounds)
                        self.assertGreaterEqual(bounds["width"], 44)
                        self.assertGreaterEqual(bounds["height"], 44)
                    self.capture(page, engine, f"analysis-overview-{label}")
                page.get_by_role("link", name="Jump to latest records").press("Enter")
                expect(latest).to_be_in_viewport()
                self.capture(page, engine, "case-tools-latest-reflow")
                page.get_by_role("link", name="Jump to analysis methods").press("Enter")
                expect(methods).to_be_in_viewport()
                self.capture(page, engine, "case-tools-methods-reflow")
                trips = directory.get_by_role("link", name="Trips", exact=True)
                trips.focus()
                expect(trips).to_be_focused()
                trips.press("Enter")
                expect(page).to_have_url(self.url("geo:case_map", case_id=self.case.pk))
                expect(
                    page.get_by_role("heading", name="Trips", exact=True)
                ).to_be_visible()
                page.go_back()
                expect(reports).to_be_visible()
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self._assert_reflow(page)
                self.capture(page, engine, "analysis-overview-large-text")
                page.goto(index_url)
                page.set_viewport_size({"width": 1440, "height": 900})
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                page.reload()
                self.capture(page, engine, "analysis-overview-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                page.reload()
                self.capture(page, engine, "analysis-overview-forced-colors")
                report_link = page.get_by_role(
                    "link", name=re.compile(r"^Call patterns")
                )
                report_link.focus()
                expect(report_link).to_be_focused()
                report_link.press("Enter")
                expect(
                    page.get_by_role("heading", name="Call patterns", exact=True)
                ).to_be_visible()
                page.emulate_media(
                    color_scheme="light",
                    forced_colors="none",
                    reduced_motion="no-preference",
                )
                page.goto(index_url)
                chooser_link = methods.get_by_role(
                    "link", name="Browse methods by evidence type", exact=True
                )
                chooser_link.focus()
                expect(chooser_link).to_be_focused()
                chooser_link.press("Enter")
                expect(
                    page.get_by_role("heading", name="Choose an analysis")
                ).to_be_visible()
                expect(page.locator(".analysis-assumptions")).to_have_count(0)
                expect(page.locator('input[name="sources"]')).to_have_count(0)
                choices = page.get_by_role("navigation", name="Available analysis")
                expect(choices.locator("details")).to_have_count(0)
                for choice in choices.get_by_role("link").all():
                    expect(choice).to_be_visible()
                self.capture(page, engine, "analysis-chooser")
                task = page.get_by_role(
                    "link", name="Extract document text", exact=True
                )
                expect(task).to_be_visible()
                for width, height, label in (
                    (1440, 900, "desktop"),
                    (390, 844, "mobile"),
                    (320, 844, "reflow"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.assertGreaterEqual(
                        task.bounding_box()["height"],
                        44,
                    )
                    self.capture(page, engine, f"analysis-selection-{label}")
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                self._assert_reflow(page)
                self.assertGreaterEqual(
                    task.bounding_box()["height"],
                    88,
                )
                self.assertGreater(
                    page.locator(".case-context").bounding_box()["width"],
                    page.viewport_size["width"] / 2,
                )
                brand = page.locator(".app-rail-brand")
                self.assertGreaterEqual(brand.bounding_box()["width"], 44)
                self.assertGreaterEqual(brand.bounding_box()["height"], 44)
                self.assertLessEqual(
                    page.get_by_role("navigation", name="Investigation")
                    .get_by_role("link", name="Evidence", exact=True)
                    .bounding_box()["height"],
                    128,
                )
                self.capture(page, engine, "analysis-selection-large-text")
                task.focus()
                expect(task).to_be_focused()
                task.press("Enter")
                expect(
                    page.get_by_role(
                        "heading", name="Extract document text", exact=True
                    )
                ).to_be_visible()
                expect(
                    page.get_by_label(f"Document · {self.document.title}", exact=True)
                ).to_be_visible()
                page.get_by_role("link", name="Change analysis", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Choose an analysis", exact=True)
                ).to_be_visible()

    def test_external_rerun_uses_current_case_approval_without_javascript(self):
        gateway = settings.PANDECT_AI_GATEWAY | {
            "account": "a" * 32,
            "gateway": "synthetic-rerun",
            "gateway_token": "synthetic-gateway-key",
            "workers_token": "synthetic-workers-key",
            "models": {"text": "@cf/synthetic/reviewed-model"},
        }
        with self.settings(PANDECT_AI_GATEWAY=gateway):
            prior, _ = start_run(
                self.owner,
                self.case.pk,
                "content_events",
                [{"kind": "text", "id": str(self.text.pk)}],
                parameters={"backend": "gateway"},
                **external_grant_kwargs(self.case, "content_events"),
            )
            cancel_run(self.owner, self.case.pk, prior.pk)
            detail_url = self.url(
                "core:analysis:detail", case_id=self.case.pk, run_id=prior.pk
            )
            for engine in self.engines:
                with (
                    self.subTest(engine=engine),
                    self.browser_page(engine, java_script_enabled=False) as (_, page),
                ):
                    self.db(external_grant_kwargs, self.case, "content_events")
                    self.sign_in(page)
                    page.goto(detail_url)
                    page.get_by_text("Details and history", exact=True).press("Enter")
                    expect(
                        page.locator('input[name="external_processing"]')
                    ).to_have_count(0)
                    submit = page.get_by_role(
                        "button", name="Start another run with these inputs"
                    )
                    next_control = (
                        "Alt+Tab"
                        if engine == "webkit" and sys.platform == "darwin"
                        else "Tab"
                    )
                    page.get_by_text("Method and settings", exact=True).focus()
                    page.keyboard.press(next_control)
                    expect(submit).to_be_focused()
                    self.assertNotEqual(
                        submit.evaluate("node => getComputedStyle(node).outlineStyle"),
                        "none",
                    )
                    for width, height, label in (
                        (1440, 900, "desktop"),
                        (1440, 1024, "tall-desktop"),
                        (768, 1024, "tablet"),
                        (390, 844, "mobile"),
                        (320, 844, "reflow"),
                    ):
                        page.set_viewport_size({"width": width, "height": height})
                        self._assert_reflow(page)
                        submit.scroll_into_view_if_needed()
                        self.capture(page, engine, f"external-rerun-{label}")
                    page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                    self.capture(page, engine, "external-rerun-dark")
                    if engine == "chromium":
                        page.emulate_media(color_scheme="light", forced_colors="active")
                        self.capture(page, engine, "external-rerun-forced-colors")
                    submit.press("Enter")
                    expect(page).not_to_have_url(detail_url)
                    expect(
                        page.get_by_role("heading", name="Queued", exact=True)
                    ).to_be_visible()
                    self.assertTrue(
                        self.db(
                            AnalysisRun.objects.filter(
                                previous_run=prior, external_processing=True
                            ).exists
                        )
                    )
                    self.db(revoke_case_execution, self.case)
                    page.goto(detail_url)
                    expect(submit).to_have_count(0)
                    expect(
                        page.get_by_role("link", name="Case settings").last
                    ).to_be_visible()

    def test_topic_failure_retains_keywords_and_keyboard_retry_without_javascript(self):
        with self.settings(PANDECT_LOCAL_AI=LOCAL):
            run, _ = start_run(
                self.owner,
                self.case.pk,
                "text_topics",
                [{"kind": "text", "id": str(self.text.pk)}],
                parameters={"include_summary": True},
            )
            client = Mock(usage={"request_count": 1})
            client.infer.return_value = {"items": "PRIVATE UNVALIDATED RESPONSE"}
            with patch("core.analysis.local.LocalClient", return_value=client):
                process_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, "partial", run.error_code)
            result_id = run.result.pk
            detail_url = self.url(
                "core:analysis:detail", case_id=self.case.pk, run_id=run.pk
            )
            for engine in self.engines:
                with (
                    self.subTest(engine=engine),
                    self.browser_page(engine, java_script_enabled=False) as (_, page),
                ):
                    self.sign_in(page)
                    page.goto(detail_url)
                    expect(
                        page.get_by_role("status").filter(
                            has_text=re.compile(r"^Partial$")
                        )
                    ).to_be_visible()
                    expect(
                        page.get_by_role(
                            "heading",
                            name=(
                                "Keywords retained; topic suggestions "
                                "were not published."
                            ),
                            exact=True,
                        )
                    ).to_be_visible()
                    expect(
                        page.get_by_role("heading", name="Findings", exact=True)
                    ).to_be_visible()
                    expect(page.locator("main")).not_to_contain_text(
                        "Processing could not finish"
                    )
                    expect(page.locator("main")).not_to_contain_text(
                        "PRIVATE UNVALIDATED RESPONSE"
                    )
                    limitations = page.get_by_text("Limitations", exact=True)
                    expect(
                        page.get_by_role("link", name="Download results", exact=True)
                    ).to_be_hidden()
                    expect(
                        page.get_by_role(
                            "button",
                            name="Start another run with these inputs",
                            exact=True,
                        )
                    ).to_be_hidden()
                    limitations.focus()
                    expect(page.locator(".analysis-result-limits")).to_have_attribute(
                        "open", ""
                    )
                    expect(
                        page.get_by_text(
                            "Topic suggestions could not be validated "
                            "for this selection.",
                            exact=False,
                        )
                    ).to_be_visible()
                    for width, height, label in (
                        (1440, 900, "desktop"),
                        (1440, 1024, "tall-desktop"),
                        (768, 1024, "tablet"),
                        (390, 844, "mobile"),
                        (320, 844, "reflow"),
                    ):
                        page.set_viewport_size({"width": width, "height": height})
                        for scheme in ("light", "dark"):
                            page.emulate_media(
                                color_scheme=scheme, reduced_motion="reduce"
                            )
                            self._assert_reflow(page)
                            self.capture(
                                page, engine, f"topic-recovery-{label}-{scheme}"
                            )
                    page.set_viewport_size({"width": 1440, "height": 900})
                    page.get_by_text("Details and history", exact=True).press("Enter")
                    retry = page.get_by_role(
                        "button", name="Start another run with these inputs", exact=True
                    )
                    retry.focus()
                    expect(retry).to_be_focused()
                    retry.press("Enter")
                    expect(page).not_to_have_url(detail_url)
                    expect(
                        page.get_by_role("heading", name="Queued", exact=True)
                    ).to_be_visible()
                    retried_id = UUID(page.url.rstrip("/").split("/")[-1])
                    retried = self.db(AnalysisRun.objects.get, pk=retried_id)
                    self.assertEqual(retried.previous_run_id, run.pk)
                    self.assertEqual(
                        self.db(AnalysisRun.objects.get, pk=run.pk).status, "partial"
                    )
                    self.assertEqual(
                        self.db(lambda: AnalysisRun.objects.get(pk=run.pk).result.pk),
                        result_id,
                    )
                with self.browser_page(engine, java_script_enabled=False) as (_, page):
                    self.sign_in(page, self.viewer)
                    page.goto(detail_url)
                    expect(
                        page.get_by_role("heading", name="Findings", exact=True)
                    ).to_be_visible()
                    page.get_by_text("Details and history", exact=True).press("Enter")
                    expect(
                        page.get_by_role(
                            "button", name="Start another run with these inputs"
                        )
                    ).to_have_count(0)

    def test_paginated_findings_keep_source_links_without_javascript(
        self,
    ):
        run_id = self._many_findings()
        run_url = self.url("core:analysis:detail", case_id=self.case.pk, run_id=run_id)
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(run_url)
                page.get_by_role(
                    "navigation", name="Finding pages", exact=True
                ).get_by_role("link", name="Next findings", exact=True).press("Enter")
                expect(page).to_have_url(re.compile(r"\?page=2(?:#.*)?$"))
                for number in (51, 52):
                    finding = page.locator(f"#finding-{number}")
                    expect(
                        finding.get_by_role("link", name=self.text.title)
                    ).to_be_visible()
                    expect(finding.locator(".analysis-finding-text")).to_be_visible()
                row = page.locator("#finding-link-52")
                expect(row).to_have_accessible_name("Email · contact-52@example.test")
                row.focus()
                expect(row).to_be_focused()
                row.press("Enter")
                expect(page).to_have_url(re.compile(r"\?page=2&finding=52#finding-52$"))
                inspector = page.locator("#finding-52")
                expect(
                    inspector.get_by_role("link", name=self.text.title)
                ).to_be_visible()
                expect(
                    page.get_by_text("Review this finding", exact=True)
                ).to_have_count(0)
                expect(
                    page.get_by_text("Review and history", exact=True)
                ).to_have_count(0)
                page.evaluate("window.scrollTo(0, 0)")
                self.capture(page, engine, "analysis-findings-desktop")
                page.go_back()
                expect(page).to_have_url(re.compile(r"\?page=2(?:#.*)?$"))
                expect(
                    page.locator("#finding-51 .analysis-finding-text")
                ).to_be_visible()

                page.set_viewport_size({"width": 320, "height": 844})
                row.press("Enter")
                self._assert_reflow(page)
                expect(
                    inspector.get_by_role("link", name=self.text.title)
                ).to_be_visible()
                self.capture(page, engine, "analysis-findings-mobile-detail")
                back = page.get_by_role(
                    "link", name="All findings in this result", exact=True
                )
                back.press("Enter")
                expect(page).to_have_url(re.compile(r"\?page=2#finding-link-52$"))
                expect(row).to_be_visible()
                self._assert_reflow(page)
                self.capture(page, engine, "analysis-findings-mobile-list")

    def test_review_priority_across_evidence_catalogue_and_run(self):
        target = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Pickup instructions",
            body="office@example.test " * 55 + "Booking REF-1847",
        )
        receipt = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Pickup receipt",
            body="Receipt for booking REF-1847",
        )
        for original in (receipt, target):
            run, _ = start_run(
                self.owner,
                self.case.pk,
                "entities",
                [{"kind": "text", "id": str(original.pk)}],
            )
            self._complete(run.pk)
        reason = (
            "The same identifier appears in another original with different content."
        )
        evidence_url = self.url(
            "core:evidence:detail",
            case_id=self.case.pk,
            kind="text",
            evidence_id=target.pk,
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(evidence_url)
                first = page.locator(
                    "#evidence-findings .analysis-finding-rows li"
                ).first
                expect(first).to_contain_text("REF-1847")
                expect(first).to_contain_text(reason)
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 844),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    first.scroll_into_view_if_needed()
                    self.capture(page, engine, f"review-priority-{width}-{height}")
                page.emulate_media(color_scheme="dark", reduced_motion="reduce")
                self.capture(page, engine, "review-priority-dark")
                page.emulate_media(color_scheme="light", forced_colors="active")
                self.capture(page, engine, "review-priority-forced-colors")
                page.get_by_role("link", name="Next findings", exact=True).press(
                    "Enter"
                )
                row = page.locator("#evidence-findings .analysis-finding-rows li").first
                origin = row.get_attribute("id")
                row.locator("a").first.press("Enter")
                page.get_by_role("link", name="Back to evidence", exact=True).press(
                    "Enter"
                )
                expect(page).to_have_url(
                    re.compile(r"findings_page=2#" + re.escape(origin) + "$")
                )
                page.get_by_role("link", name="Previous findings", exact=True).press(
                    "Enter"
                )
                first.locator("a").first.press("Enter")
                expect(page.locator(".analysis-finding").first).to_contain_text(
                    "REF-1847"
                )
                page.get_by_role("link", name="Compare matching sources").press("Enter")
                expect(
                    page.get_by_role("link", name="Pickup receipt", exact=True)
                ).to_be_visible()
                page.go_back()
                page.get_by_role(
                    "link", name="All findings in this result", exact=True
                ).press("Enter")
                first = page.locator(".analysis-findings-table tbody tr").first
                expect(first).to_contain_text("REF-1847")
                expect(first).to_contain_text(reason)

    def test_image_metadata_shows_values_by_source_without_javascript(self):
        titles = ("Synthetic north camera", "Synthetic south camera")
        makes = ("<b>Synthetic make A</b>", "Synthetic make B")
        inputs = []
        for title, make in zip(titles, makes, strict=True):
            image = Image.new("RGB", (80, 60))
            exif = image.getexif()
            exif[271] = make
            exif[272] = "Synthetic model"
            exif[274] = 1
            contents = BytesIO()
            image.save(contents, "JPEG", exif=exif)
            evidence = ImageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=title,
                file=ContentFile(contents.getvalue(), name="metadata.jpg"),
            )
            inputs.append({"kind": "image", "id": str(evidence.pk)})
        run, _ = start_run(self.owner, self.case.pk, "image_metadata", inputs)
        self._complete(run.pk)
        url = self.url("core:analysis:detail", case_id=self.case.pk, run_id=run.pk)

        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(url)
                decoded = page.get_by_role(
                    "table", name="Decoded image and metadata claims", exact=True
                )
                expect(
                    decoded.first.get_by_role("rowheader", name="Decoded width")
                ).to_be_visible()
                expect(
                    decoded.first.get_by_role("cell", name="80", exact=True)
                ).to_be_visible()
                for title, make in zip(titles, makes, strict=True):
                    tags = page.get_by_role(
                        "table", name=f"Embedded metadata · {title}"
                    )
                    expect(
                        tags.get_by_role("cell", name=make, exact=True)
                    ).to_be_visible()
                    expect(
                        tags.get_by_role("cell", name="Synthetic model")
                    ).to_be_visible()
                    expect(
                        tags.get_by_role("cell", name="1", exact=True)
                    ).to_be_visible()
                tags = page.get_by_role(
                    "table", name=f"Embedded metadata · {titles[0]}"
                )
                make_link = tags.get_by_role("link", name="Make", exact=True)
                for width, height, label in (
                    (1440, 900, "desktop"),
                    (320, 844, "reflow"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    tags.scroll_into_view_if_needed()
                    self.capture(page, engine, f"analysis-metadata-{label}")
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "analysis-metadata-dark")
                make_link.focus()
                expect(make_link).to_be_focused()
                make_link.press("Enter")
                expect(
                    page.get_by_role("heading", name="Raw metadata · Make")
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name=titles[0], exact=True)
                ).to_be_visible()
                page.get_by_role("link", name="All findings in this result").press(
                    "Enter"
                )
                selected = tags.get_by_role("row").filter(has_text=makes[0])
                expect(
                    selected.get_by_role("cell", name=makes[0], exact=True)
                ).to_be_visible()
                expect(
                    tags.get_by_role("columnheader", name="Review", exact=True)
                ).to_have_count(0)

    def test_recording_intervals_and_current_record_tables_without_javascript(self):
        content = wav_bytes(seconds=2, value=0)
        recording = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic recorder",
            file=ContentFile(content, name="recorder.wav"),
        )
        AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Archived recorder copy",
            file=ContentFile(content, name="archived-copy.wav"),
            is_archived=True,
            archive_changed_at=datetime.now(UTC),
            archive_changed_by=self.owner,
        )
        call = PhoneCallEvidence.objects.get(case=self.case)
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            phone_call=call,
            transcript="Synthetic supplied call wording",
            file=ContentFile(content, name="retained-call.wav"),
        )
        run, _ = start_run(
            self.owner,
            self.case.pk,
            "audio_metadata",
            [
                {"kind": "audio", "id": str(recording.pk)},
                {
                    "kind": "phone-call",
                    "id": str(call.pk),
                    "attachment_id": str(addition.pk),
                },
            ],
        )
        self._complete(run.pk)
        stamp = datetime(2026, 10, 27, 12, tzinfo=UTC)
        vehicle = Vehicle.objects.create(case=self.case, make="Synthetic vehicle")
        observation = VehicleLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic depot observation",
            vehicle=vehicle,
            address="Recorded depot",
            observed_at=stamp,
            received_at=stamp + timedelta(seconds=7),
            speed_kmh=0,
        )

        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(
                    self.url(
                        "core:analysis:detail", case_id=self.case.pk, run_id=run.pk
                    )
                )
                expect(page.locator("audio")).to_have_count(2)
                expect(
                    page.get_by_role("link", name="Play 0.0–1.0 seconds")
                ).to_have_count(2)
                expect(
                    page.get_by_role("heading", name=re.compile("retained-call.wav"))
                ).to_be_visible()
                audio_url = page.locator("audio").first.get_attribute("src")
                audio = page.request.get(self.live_server_url + audio_url)
                self.assertEqual(audio.status, 200)
                self.assertIn("private", audio.headers["cache-control"])
                self.assertEqual(audio.body(), content)
                for width, height in ((1440, 900), (320, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    page.locator("audio").first.scroll_into_view_if_needed()
                    self.capture(page, engine, f"analysis-audio-results-{width}")

                for name, field in (
                    ("vehicle-coverage", "Receipt minus event (seconds)"),
                    ("vehicle-speed", "Reported speed (km/h)"),
                    ("duplicates", None),
                ):
                    page.goto(
                        self.live_server_url
                        + reverse("core:analysis:report", args=[self.case.pk, name])
                    )
                    table = page.get_by_role("table")
                    expect(table).to_have_count(1)
                    if field:
                        expect(
                            table.get_by_text(field, exact=True).first
                        ).to_be_visible()
                    if name == "duplicates":
                        expect(table).to_have_accessible_name(
                            "Group 1 · 2 identical files"
                        )
                        for filename in ("recorder.wav", "retained-call.wav"):
                            expect(
                                table.get_by_role("link", name=filename)
                            ).to_be_visible()
                        expect(
                            table.get_by_role("link", name="archived-copy.wav")
                        ).to_have_count(0)
                        details = table.locator("summary").first
                        details.press("Enter")
                        expect(
                            table.get_by_text("Source type", exact=True).first
                        ).to_be_visible()
                        expect(
                            table.get_by_text("Byte size", exact=True).first
                        ).to_be_visible()
                        details.press("Enter")
                    if name == "vehicle-coverage":
                        observed = page.get_by_role(
                            "table", name="Report records"
                        ).locator('time[datetime="2026-10-27T12:00:00+00:00"]')
                        expect(observed).to_have_text("27 Oct 2026, 12:00:00 UTC")
                        expect(page.locator("#report-totals")).to_be_visible()
                    page.locator("#report-totals").scroll_into_view_if_needed()
                    self.capture(page, engine, f"analysis-report-totals-{name}-320")
                    self._assert_reflow(page)
                    table.scroll_into_view_if_needed()
                    self.capture(page, engine, f"analysis-report-{name}-320")

                page.goto(
                    self.url(
                        "core:analysis:process_evidence",
                        case_id=self.case.pk,
                        kind="vehicle-location",
                        evidence_id=observation.pk,
                    )
                )
                expect(page.get_by_text("Reported vehicle", exact=True)).to_be_visible()
                expect(page.get_by_text("Recorded depot", exact=True)).to_be_visible()
                expect(page.get_by_text("Receipt time", exact=True)).to_be_visible()
                expect(
                    page.get_by_text("27 Oct 2026, 12:00:07 UTC", exact=True)
                ).to_be_visible()
                self._assert_reflow(page)
                page.goto(
                    self.url(
                        "core:analysis:process_evidence",
                        case_id=self.case.pk,
                        kind="phone-call",
                        evidence_id=call.pk,
                    )
                )
                expect(
                    page.get_by_role("heading", name="retained-call.wav", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("Synthetic supplied call wording", exact=True)
                ).to_be_visible()
                expect(page.locator(f"#attachment-{addition.pk}")).to_be_visible()

    def test_native_extraction_source_anchors_and_responsive_layout(self):
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                page.goto(self.url("core:evidence:list", case_id=self.case.pk))
                page.get_by_role("navigation", name="Investigation").get_by_role(
                    "link", name="Case Tools", exact=True
                ).click()
                expect(
                    page.get_by_role("heading", name="Case Tools", exact=True)
                ).to_be_visible()
                methods = page.get_by_role("region", name="Analysis methods")
                task = methods.get_by_role(
                    "link", name="Extract document text", exact=True
                )
                task.focus()
                expect(task).to_be_focused()
                task.press("Enter")
                expect(
                    page.get_by_role(
                        "heading", name="Extract document text", exact=True
                    )
                ).to_be_visible()
                source = page.get_by_label(
                    "Document · Synthetic retained report", exact=True
                )
                source.focus()
                expect(source).to_be_focused()
                self.assertNotEqual(
                    source.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                source.press("Space")
                expect(source).to_be_checked()
                page.get_by_role("button", name="Start analysis", exact=True).click()
                expect(
                    page.get_by_role(
                        "heading", name="Extract document text", exact=True
                    )
                ).to_be_visible()
                expect(
                    page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
                ).to_be_visible()
                run_id = UUID(page.url.rstrip("/").split("/")[-1])
                self.db(self._complete, run_id)
                page.get_by_role("link", name="Refresh status", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Native UTF-8 text", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_text("source@analysis.test", exact=False)
                ).to_be_visible()
                expect(page.locator("iframe")).to_have_count(0)
                expect(
                    page.locator("#finding-1").get_by_role(
                        "link", name="Synthetic retained report", exact=True
                    )
                ).to_have_attribute("href", re.compile(r"/evidence/document/"))
                page.get_by_text("Analysis run details", exact=True).press("Enter")
                processing_summary = page.locator("summary").filter(
                    has_text=re.compile(r"^Processing summary$")
                )
                expect(
                    page.get_by_text("Selected documents", exact=True)
                ).to_be_hidden()
                processing_summary.focus()
                processing_summary.press("Enter")
                expect(
                    page.get_by_text("Selected documents", exact=True)
                ).to_be_visible()
                processing_summary.press("Enter")
                for width, height, label in (
                    (1440, 900, "desktop"),
                    (1440, 1024, "tall-desktop"),
                    (768, 1024, "tablet"),
                    (390, 844, "mobile"),
                    (320, 844, "reflow"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"analysis-result-{label}")
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "analysis-result-dark")
                page.emulate_media(
                    color_scheme="light",
                    forced_colors="active",
                    reduced_motion="reduce",
                )
                self.capture(page, engine, "analysis-result-forced-colors")
                page.emulate_media(forced_colors="none")
                page.set_viewport_size({"width": 1440, "height": 900})
                with page.expect_download() as waiting:
                    page.get_by_role(
                        "link", name="Download results", exact=True
                    ).click()
                download = waiting.value
                self.assertEqual(download.suggested_filename, f"analysis-{run_id}.json")
                data = json.loads(Path(download.path()).read_text())
                self.assertEqual(data["operation"], "native_text")
                self.assertEqual(data["inputs"][0]["sha256"], self.document.sha256)
                self.assertIn(
                    "source@analysis.test", data["result"]["items"][0]["text"]
                )
                page.locator("#finding-1").get_by_role(
                    "link", name="Synthetic retained report", exact=True
                ).click()
                expect(
                    page.get_by_role(
                        "heading", name="Synthetic retained report", exact=True
                    )
                ).to_be_visible()

    @override_settings(PANDECT_AI_GATEWAY=OPENAI, PANDECT_LOCAL_AI=LOCAL)
    def test_process_single_evidence_without_javascript_and_responsive_layout(self):
        DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Another document outside this selection",
            file=ContentFile(b"Synthetic other source.", name="other.txt"),
        )
        viewports = (
            (1440, 900, "desktop"),
            (1440, 1024, "tall-desktop"),
            (768, 1024, "tablet"),
            (390, 844, "mobile"),
            (320, 844, "reflow"),
        )
        analysis_url = self.url(
            "core:analysis:process_evidence",
            case_id=self.case.pk,
            kind="document",
            evidence_id=self.document.pk,
        )
        detail_url = self.url(
            "core:evidence:detail",
            case_id=self.case.pk,
            kind="document",
            evidence_id=self.document.pk,
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.db(AnalysisRun.objects.filter(case=self.case).delete)
                self.db(CaseAnalysisBatch.objects.filter(case=self.case).delete)
                self.sign_in(page)
                page.goto(detail_url)
                expect(page.locator("#evidence-original-heading")).to_have_count(1)
                expect(
                    page.get_by_role("heading", name="Findings", exact=True)
                ).to_be_visible()
                expect(page.locator(".evidence-info-panel")).to_be_hidden()
                for width, height, label in viewports:
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"process-evidence-action-{label}")
                process = page.get_by_role(
                    "button",
                    name=re.compile(r"^Run checks(?: again)?$"),
                )
                process.focus()
                expect(process).to_be_focused()
                self.assertNotEqual(
                    process.evaluate("node => getComputedStyle(node).outlineStyle"),
                    "none",
                )
                page.locator('input[name="submission_id"]').evaluate(
                    "input => { input.value = 'invalid'; }"
                )
                process.press("Enter")
                error = page.locator("#evidence-analysis-error")
                expect(error).to_contain_text("Checks could not start")
                expect(error).to_be_in_viewport()
                expect(error).to_be_focused()
                expect(process).to_be_visible()
                expect(page.locator("#evidence-original-heading")).to_have_count(1)
                expect(page.locator(".evidence-info-panel")).to_be_hidden()
                self.assertFalse(
                    self.db(AnalysisRun.objects.filter(case=self.case).exists)
                )
                page.goto(detail_url)
                process.press("Enter")
                expect(
                    page.get_by_role("heading", name="Processing", exact=True)
                ).to_be_visible()
                batch = self.db(CaseAnalysisBatch.objects.get, case=self.case)
                page.goto(detail_url)
                expect(
                    page.get_by_role("heading", name=self.document.title, exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("status").filter(has_text="Preparing checks…")
                ).to_be_visible()
                follow = page.get_by_role(
                    "link", name="Follow evidence analysis", exact=True
                )
                follow.focus()
                follow.press("Enter")
                expect(page.locator("#processing-history .notice")).to_contain_text(
                    "Selected batch"
                )
                for _ in range(3):
                    if self.db(prepare_case_analysis_batch, batch.pk) == "complete":
                        break
                page.goto(detail_url)
                expect(page.get_by_label("Analysis", exact=True)).to_have_count(0)
                expect(page.locator('input[name="sources"]')).to_have_count(0)
                expect(
                    page.locator(".evidence-reader").get_by_text(
                        "Another document outside this selection", exact=False
                    )
                ).to_have_count(0)
                outcomes = page.locator("#evidence-outcomes")
                outcomes.locator(":scope > summary").press("Enter")
                native = outcomes.get_by_role(
                    "link", name=re.compile("Extract document text")
                ).first
                expect(native).to_contain_text("Queued")
                expect(
                    page.get_by_role("status").filter(has_text="Checking…")
                ).to_be_visible()
                run_id = UUID(
                    urlsplit(native.get_attribute("href"))
                    .path.rstrip("/")
                    .split("/")[-1]
                )
                self.db(self._complete, run_id)
                refresh = page.get_by_role("link", name="Refresh status", exact=True)
                refresh.focus()
                expect(refresh).to_be_focused()
                refresh.press("Enter")
                if outcomes.get_attribute("open") is None:
                    outcomes.locator(":scope > summary").press("Enter")
                expect(native).to_contain_text("Complete")
                expect(
                    page.get_by_role(
                        "link", name=re.compile("Extract identifiers and dates")
                    ).first
                ).to_be_visible()
                outcomes.locator(":scope > summary").press("Enter")
                for width, height, label in viewports:
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    page.locator(
                        "#evidence-findings-heading"
                    ).scroll_into_view_if_needed()
                    self.capture(page, engine, f"process-evidence-results-{label}")
                page.emulate_media(color_scheme="dark")
                self.capture(page, engine, "process-evidence-results-dark")
                page.emulate_media(
                    color_scheme="light",
                    forced_colors="active",
                    reduced_motion="reduce",
                )
                self.capture(page, engine, "process-evidence-results-forced-colors")
                page.locator(".evidence-analyze > summary").press("Enter")
                page.get_by_role(
                    "link", name="Suggest claims and commitments", exact=True
                ).click()
                expect(
                    page.get_by_role(
                        "heading", name="Suggest claims and commitments", exact=True
                    )
                ).to_be_visible()
                expect(
                    page.get_by_label("Processing location", exact=True)
                ).to_have_count(0)
                expect(page.locator('input[name="external_processing"]')).to_have_count(
                    0
                )
                page.get_by_role(
                    "checkbox",
                    name=re.compile(r"Document · .* · Extract document text"),
                ).check()
                start = page.get_by_role("button", name="Start analysis", exact=True)
                start.focus()
                expect(start).to_be_focused()
                for width, height, label in viewports:
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"local-analysis-form-{label}")
                start.press("Enter")
                expect(
                    page.get_by_role("heading", name="Queued", exact=True)
                ).to_be_visible()
                self.assertFalse(
                    self.db(AnalysisRun.objects.filter(external_processing=True).exists)
                )
                page.goto(analysis_url)
                if outcomes.get_attribute("open") is None:
                    outcomes.locator(":scope > summary").press("Enter")
                native.click()
                expect(
                    page.get_by_role("heading", name="Native UTF-8 text", exact=True)
                ).to_be_visible()
                source_findings = page.get_by_role(
                    "link", name="All findings for this evidence", exact=True
                )
                expect(source_findings).to_be_visible()
                source_findings.focus()
                expect(source_findings).to_be_focused()
                source_findings.press("Enter")
                expect(
                    page.get_by_role("heading", name=self.document.title, exact=True)
                ).to_be_visible()
                run = self.db(AnalysisRun.objects.get, pk=run_id)
                self.assertEqual(
                    [selected["evidence_id"] for selected in run.inputs],
                    [str(self.document.pk)],
                )

    def test_viewer_reports_sources_and_access_removal_without_javascript(self):
        person = Person.objects.get(case=self.case)
        PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic archived call",
            sender=person,
            recipient=person,
            is_archived=True,
            archive_changed_at=datetime.now(UTC),
            archive_changed_by=self.owner,
        )
        for engine in self.engines:
            # Each browser engine receives fresh viewer membership after the prior
            # engine's revocation has been checked.
            CaseMembership.objects.get_or_create(
                case=self.case, user=self.viewer, defaults={"role": "viewer"}
            )
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.viewer)
                page.goto(self.url("core:analysis:index", case_id=self.case.pk))
                expect(
                    page.get_by_role("region", name="Analysis methods")
                ).to_have_count(0)
                page.get_by_role("link", name="Call patterns", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Call patterns", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role(
                        "link",
                        name="Phone call · Synthetic metadata-only call",
                        exact=True,
                    )
                ).to_be_visible()
                expect(page.get_by_text("Selected records", exact=True)).to_be_visible()
                pairs = page.locator('section[aria-labelledby="report-pairs"]')
                expect(pairs).to_contain_text(str(person))
                expect(pairs).not_to_contain_text(str(person.pk))
                expect(
                    page.get_by_text("Reported duration (seconds)", exact=True).first
                ).to_be_visible()
                participant = page.get_by_label("Participant", exact=True)
                expect(participant).to_be_hidden()
                self.capture(page, engine, "analysis-report-default")
                report_filters = page.locator(".report-filters > summary")
                report_filters.focus()
                report_filters.press("Enter")
                expect(participant).to_be_visible()
                expect(page.get_by_label("State", exact=True)).to_have_count(0)
                participant.select_option(str(person.pk))
                page.get_by_role("button", name="Update report", exact=True).click()
                expect(
                    page.get_by_role(
                        "link",
                        name="Phone call · Synthetic metadata-only call",
                        exact=True,
                    )
                ).to_be_visible()
                expect(page.locator(".report-filters")).not_to_have_attribute(
                    "open", ""
                )
                expect(report_filters).to_contain_text(str(person))
                expect(
                    page.get_by_role(
                        "link", name="Phone call · Synthetic archived call", exact=True
                    )
                ).to_have_count(0)
                details = page.locator("details").filter(
                    has=page.locator("summary", has_text="About this report")
                )
                details.locator(":scope > summary").press("Enter")
                expect(details).to_contain_text(str(person))
                expect(details).not_to_contain_text(str(person.pk))
                self.capture(page, engine, "analysis-viewer-call-report")
                report_filters.press("Enter")
                page.get_by_label("From (UTC)", exact=True).fill("2026-11-02T10:00")
                page.get_by_label("Through (UTC)", exact=True).fill("2026-11-01T10:00")
                page.get_by_role("button", name="Update report", exact=True).click()
                expect(page.locator(".report-filters")).to_have_attribute("open", "")
                expect(page.locator(".error-summary")).to_contain_text(
                    "The end must follow the start."
                )
                self.db(self._revoke_viewer)
                response = page.reload()
                self.assertEqual(response.status, 404)
                expect(
                    page.get_by_text("Synthetic metadata-only call", exact=True)
                ).to_have_count(0)

    def _phase3_sources(self):
        writer = Person.objects.create(case=self.case, first_name="Synthetic writer")
        recipient = Person.objects.create(
            case=self.case, first_name="Synthetic recipient"
        )
        start = datetime(2026, 10, 1, 12, tzinfo=UTC)
        locations = [
            PersonLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Synthetic visit sample {index + 1}",
                person=writer,
                latitude=Decimal("45.501700"),
                longitude=Decimal("-73.567300"),
                observed_at=start + timedelta(minutes=minute),
            )
            for index, minute in enumerate((0, 10, 180, 190))
        ]
        excluded = "COPIED SOURCE INTRODUCTION"
        wording = (
            "Hello, our team recorded the proposed harbour delivery before the "
            "scheduled meeting. Please check the package address and confirm "
            "which entrance remains available tomorrow. We also need the full "
            "inspection notes because several separate questions are still "
            "unresolved. Thanks for sending the original documents promptly."
        )
        writing = [
            [
                TextMessageEvidence.objects.create(
                    case=self.case,
                    uploaded_by=self.owner,
                    title=f"Synthetic writing {group} {index + 1}",
                    sender=writer,
                    recipient=recipient,
                    body=excluded
                    + " "
                    + (wording.lower() if group == "A" else wording.upper()),
                    occurred_at=start
                    + timedelta(days=0 if group == "A" else 5, minutes=index * 2),
                )
                for index in range(3)
            ]
            for group in ("A", "B")
        ]
        exchanges = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Synthetic exchange {index + 1}",
                sender=recipient if index == 1 else writer,
                recipient=writer if index == 1 else recipient,
                body=body,
                occurred_at=start + timedelta(days=10, minutes=minute),
            )
            for index, (body, minute) in enumerate(
                (
                    ("Can you confirm the separate proposal?", 0),
                    ("That proposal remains under review.", 2),
                    ("A later selected note about the proposal.", 300),
                )
            )
        ]
        originals = [
            (type(record), record.pk, record.sha256, record.original_bytes())
            for record in locations + writing[0] + writing[1] + exchanges
        ]
        return locations, writing, exchanges, excluded, originals

    def _assert_originals_retained(self, originals):
        for model, identifier, checksum, original_bytes in originals:
            current = model.objects.get(pk=identifier)
            self.assertEqual(current.sha256, checksum)
            self.assertEqual(current.original_bytes(), original_bytes)

    def _choose_analysis(self, page, operation):
        page.goto(
            self.url("core:analysis:create", case_id=self.case.pk)
            + f"?operation={operation}"
        )
        options = page.locator(".analysis-assumptions > summary")
        if options.count():
            options.click()

    def _queue_and_finish(self, page, operation):
        page.get_by_role("button", name="Start analysis", exact=True).click()
        expect(
            page.get_by_role("status").filter(has_text=re.compile(r"^Queued$"))
        ).to_be_visible()
        run_id = UUID(page.url.rstrip("/").split("/")[-1])
        self.db(self._complete, run_id)
        page.get_by_role("link", name="Refresh status", exact=True).click()
        self.assertEqual(
            self.db(lambda: AnalysisRun.objects.get(pk=run_id).operation), operation
        )
        return run_id

    def _assert_phase3_run(self, run_id, operation, records):
        run = AnalysisRun.objects.select_related("result").get(pk=run_id)
        self.assertEqual(run.operation, operation)
        self.assertEqual(run.status, "complete", run.error_code)
        self.assertFalse(run.external_processing)
        data = run.result.data
        by_evidence = {
            selected["evidence_id"]: selected["id"] for selected in run.inputs
        }
        if operation == "candidate_visits":
            self.assertEqual(data["metrics"]["Candidate visits"], 2)
            self.assertEqual(data["metrics"]["Excluded sampling gaps"], 1)
            self.assertEqual(len(run.inputs), 4)
        elif operation == "style_change":
            for letter, selected in zip(("a", "b"), records, strict=True):
                expected = {by_evidence[str(record.pk)] for record in selected}
                self.assertEqual(set(run.parameters[f"group_{letter}"]), expected)
                coverage = next(
                    item
                    for item in data["items"]
                    if item["label"] == f"Group {letter.upper()} coverage"
                )
                self.assertGreaterEqual(coverage["values"]["words"], 100)
                self.assertEqual(coverage["values"]["sources_usable"], 3)
                self.assertEqual(len(coverage["values"]["excluded_spans"]), 3)
                self.assertTrue(
                    all(
                        run.parameters["contexts"][key]["language"] == "en"
                        for key in expected
                    )
                )
            self.assertIsNotNone(data["metrics"]["possible_change_interval"])
        else:
            self.assertEqual(
                run.parameters["break_before"], [by_evidence[str(records[1].pk)]]
            )
            self.assertEqual(
                run.parameters["join_before"], [by_evidence[str(records[2].pk)]]
            )
            self.assertIn("compare_split_at", run.parameters)
            self.assertEqual(data["metrics"]["inferred_sessions"], 2)
            self.assertEqual(data["metrics"]["response_intervals_seconds"], [298 * 60])
            self.assertTrue(
                any(
                    item["label"] == "At/after selected split" for item in data["items"]
                )
            )

    def _run_identifiers(self, run_id):
        run = AnalysisRun.objects.get(pk=run_id)
        return [
            identifier
            for selected in run.inputs
            for identifier in (selected["id"], selected["evidence_id"])
        ]

    def _assert_readable_findings(self, page, run_id):
        visible = page.locator("body").inner_text()
        for raw_label in ("source_id", "sender_id", "recipient_id", "[{'", "{'source"):
            self.assertNotIn(raw_label, visible)
        for identifier in self.db(self._run_identifiers, run_id):
            self.assertNotIn(identifier, visible)

    def _capture_phase3(self, page, engine, name):
        finding = page.locator("#finding-1")
        for width, height, label in ((1440, 900, "desktop"), (390, 844, "mobile")):
            page.set_viewport_size({"width": width, "height": height})
            self._assert_reflow(page)
            page.evaluate("window.scrollTo(0, 0)")
            self.capture(page, engine, f"phase3-{name}-{label}")
            finding.screenshot(
                path=str(self.artifacts / f"{engine}-phase3-{name}-finding-{label}.png")
            )
        page.set_viewport_size({"width": 1440, "height": 900})

    @override_settings(
        **LOCAL_GEOCODER,
        PANDECT_MAP_TILE_URL="https://tiles.example.test/{z}/{x}/{y}.png",
        PANDECT_MAP_TILE_ORIGIN="https://tiles.example.test",
        PANDECT_MAP_TILE_ATTRIBUTION="Synthetic map attribution",
    )
    def test_observation_basemaps_load_automatically_and_keep_coordinates(self):
        observation = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic address observation",
            address="10 Example Street",
        )
        candidate = {
            "latitude": 43.65,
            "longitude": -79.39,
            "address": "Synthetic north candidate",
            "granularity": "street",
            "reference_id": "way:1",
            "licence": "Synthetic licence",
        }
        candidates = [
            candidate,
            candidate
            | {
                "latitude": 43.64,
                "address": "Synthetic south candidate",
                "reference_id": "way:2",
            },
        ]
        tile_url = "https://tiles.example.test/**"
        tile = BytesIO()
        Image.new("RGB", (256, 256), "#d9e5d7").save(tile, format="PNG")
        tile_bytes = tile.getvalue()
        tile_requests = []

        def serve_tile(route):
            tile_requests.append(route.request.url)
            route.fulfill(status=200, content_type="image/png", body=tile_bytes)

        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine) as (context, page),
            ):
                run, _ = self.db(
                    start_run,
                    self.owner,
                    self.case.pk,
                    "geocode_locations",
                    [{"kind": "location", "id": str(observation.pk)}],
                )
                run_url = self.url(
                    "core:analysis:detail", case_id=self.case.pk, run_id=run.pk
                )
                finding_path = finding_url(self.case.pk, run.pk, 0)
                tile_requests.clear()
                context.route(tile_url, serve_tile)
                self.sign_in(page, self.viewer)
                page.goto(run_url)
                positions = page.locator("details.analysis-position-map")
                map_region = positions.get_by_role("region", name="Observation map")
                status = positions.get_by_role("status")
                expect(map_region).to_have_count(0)
                self.assertEqual(tile_requests, [])
                page.evaluate("window.liveSentinel = 42")
                with patch.object(Geocoder, "lookup", return_value=candidates):
                    self.db(self._complete, run.pk)
                expect(page.locator("#analysis-result")).to_have_attribute(
                    "data-live-poll", "false", timeout=15000
                )
                expect(map_region).to_be_visible()
                expect(status).to_have_text("Map loaded.")
                self.assertEqual(page.evaluate("window.liveSentinel"), 42)
                overview_tiles = len(tile_requests)
                self.assertGreater(overview_tiles, 0)

                coordinates = positions.locator("details").filter(
                    has=page.get_by_text("Coordinates and sources", exact=True)
                )
                coordinate_summary = coordinates.locator("summary")
                coordinate_summary.press("Enter")
                source = coordinates.get_by_role(
                    "link", name=candidate["address"], exact=True
                )
                expect(coordinates.locator("svg")).to_be_hidden()
                expect(source).to_have_attribute("href", finding_path)

                source.press("Enter")
                expect(page).to_have_url(self.live_server_url + finding_path)
                expect(coordinates.locator("svg")).to_have_count(0)
                coordinate_summary.press("Enter")
                expect(source).to_be_visible()
                expect(coordinates).to_contain_text(re.compile(r"43\.65.*-79\.39"))
                expect(
                    positions.get_by_role("button", name="Show background map")
                ).to_have_count(0)
                expect(map_region).to_be_visible()
                expect(status).to_have_text("Map loaded.")
                self.assertGreater(len(tile_requests), overview_tiles)
                marker = map_region.get_by_role(
                    "button", name=re.compile(r"^Lookup candidate")
                )
                expect(marker).to_have_class(
                    re.compile(r"\bobservation-marker-candidate\b")
                )
                for width, height, label in (
                    (1440, 900, "desktop"),
                    (1440, 1024, "tall-desktop"),
                    (768, 1024, "tablet"),
                    (390, 844, "mobile"),
                    (320, 844, "reflow"),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    if label in {"desktop", "mobile"}:
                        page.emulate_media(
                            color_scheme="dark" if label == "mobile" else "light",
                            reduced_motion="reduce",
                        )
                        map_region.scroll_into_view_if_needed()
                        expect(marker).to_be_in_viewport()
                        expect(status).to_have_text("Map loaded.")
                        self.capture(page, engine, f"observation-map-{label}")
                position_summary = positions.locator(":scope > summary")
                position_summary.press("Enter")
                expect(map_region).to_be_hidden()
                position_summary.press("Enter")
                map_region.scroll_into_view_if_needed()
                expect(marker).to_be_in_viewport()
                marker.press("Enter")
                expect(
                    map_region.get_by_role(
                        "link", name=candidate["address"], exact=True
                    )
                ).to_have_attribute("href", finding_path)

                context.unroute(tile_url, serve_tile)
                context.route(tile_url, lambda route: route.abort())
                page.reload()
                expect(status).to_contain_text("map tiles are unavailable")
                coordinate_summary.press("Enter")
                expect(source).to_be_visible()

    def test_phase3_visits_style_and_adjusted_communication_forms(self):
        locations, writing, exchanges, excluded, originals = self._phase3_sources()
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                self._choose_analysis(page, "candidate_visits")
                for record in locations:
                    page.get_by_label(
                        f"Person location · {record.title}", exact=True
                    ).check()
                page.get_by_label("Place grouping distance (metres)", exact=True).fill(
                    "100"
                )
                page.get_by_label("Observation/session gap (minutes)", exact=True).fill(
                    "60"
                )
                page.get_by_label("Minimum observed stop (minutes)", exact=True).fill(
                    "5"
                )
                visits = self._queue_and_finish(page, "candidate_visits")
                self.db(self._assert_phase3_run, visits, "candidate_visits", locations)
                positions = page.locator("details.analysis-position-map")
                coordinates = positions.locator("details").filter(
                    has=page.get_by_text("Coordinates and sources", exact=True)
                )
                expect(coordinates).to_be_visible()
                expect(
                    coordinates.get_by_text("Reported position").first
                ).to_be_visible()
                expect(
                    coordinates.get_by_role("link", name=locations[0].title, exact=True)
                ).to_be_visible()
                expect(positions.locator("svg.analysis-map")).to_have_count(0)
                expect(
                    positions.get_by_role("region", name="Observation map")
                ).to_be_hidden()
                positions.locator(":scope > summary").press("Enter")
                first = page.locator("#finding-1")
                expect(
                    first.get_by_role(
                        "heading", name="Candidate visit to place 1", exact=True
                    )
                ).to_be_visible()
                first.get_by_role("link", name="Candidate visit to place 1").click()
                expect(
                    page.locator("#finding-1").get_by_text(
                        "Synthetic writer", exact=True
                    )
                ).to_be_visible()
                self._assert_readable_findings(page, visits)
                self._capture_phase3(page, engine, "visits")

                self._choose_analysis(page, "style_change")
                for record in writing[0] + writing[1]:
                    page.get_by_label(
                        f"Text message · {record.title}", exact=True
                    ).check()
                page.get_by_label("First writing sample", exact=True).select_option(
                    [f"text-message:{record.pk}" for record in writing[0]]
                )
                page.get_by_label("Second writing sample", exact=True).select_option(
                    [f"text-message:{record.pk}" for record in writing[1]]
                )
                page.get_by_label(
                    "Quoted or copied wording to exclude", exact=True
                ).fill(excluded)
                page.get_by_label("Writing sample context", exact=True).select_option(
                    "original"
                )
                page.get_by_label("Reported language or languages", exact=True).fill(
                    "en"
                )
                page.get_by_label("Writing channel", exact=True).fill(
                    "Synthetic message export"
                )
                page.get_by_label("Writing topic", exact=True).fill("Logistics")
                page.get_by_label("Second sample channel", exact=True).fill(
                    "Synthetic message export"
                )
                page.get_by_label("Second sample topic", exact=True).fill(
                    "Formal logistics"
                )
                style = self._queue_and_finish(page, "style_change")
                self.db(self._assert_phase3_run, style, "style_change", writing)
                expect(
                    page.get_by_role(
                        "link",
                        name=re.compile(r"^Possible observed-style change interval"),
                    )
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name=re.compile(r"Group A coverage"))
                ).to_be_visible()
                self._assert_readable_findings(page, style)
                examples = page.locator("#finding-1 .analysis-example")
                quotes = examples.locator("blockquote").all_text_contents()
                self.assertIn("hello", quotes)
                self.assertIn("H", quotes)
                expected_links = {
                    record.title: self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="text-message",
                        evidence_id=record.pk,
                    ).removeprefix(self.live_server_url)
                    for group in writing
                    for record in group
                }
                example_links = examples.get_by_role("link").all()
                self.assertGreaterEqual(len(example_links), 2)
                for link in example_links:
                    title = link.inner_text()
                    self.assertIn(title, expected_links)
                    self.assertEqual(link.get_attribute("href"), expected_links[title])
                self._capture_phase3(page, engine, "style")

                self._choose_analysis(page, "comm_patterns")
                for record in exchanges:
                    page.get_by_label(
                        f"Text message · {record.title}", exact=True
                    ).check()
                page.get_by_label("Observation/session gap (minutes)", exact=True).fill(
                    "120"
                )
                page.get_by_label(
                    "Start an inferred session before", exact=True
                ).select_option(f"text-message:{exchanges[1].pk}")
                page.get_by_label(
                    "Continue an inferred session before", exact=True
                ).select_option(f"text-message:{exchanges[2].pk}")
                page.get_by_label(
                    "Compare communications before and after (UTC)", exact=True
                ).fill("2026-10-11T12:01")
                timing = self._queue_and_finish(page, "comm_patterns")
                self.db(self._assert_phase3_run, timing, "comm_patterns", exchanges)
                expect(
                    page.get_by_role("link", name=re.compile(r"Observed session 2"))
                ).to_be_visible()
                expect(
                    page.get_by_role("link", name="At/after selected split")
                ).to_be_visible()
                self._assert_readable_findings(page, timing)
                first_exchange = page.locator("#finding-1")
                expect(
                    first_exchange.get_by_text("Synthetic writer", exact=True)
                ).to_be_visible()
                expect(
                    first_exchange.get_by_text("Synthetic recipient", exact=True)
                ).to_be_visible()
                self._capture_phase3(page, engine, "communications")
                report_url = self.url(
                    "core:analysis:detail", case_id=self.case.pk, run_id=timing
                )
                page.goto(report_url + "?summary=1#processing-summary")
                summary = page.locator("#processing-summary")
                expect(summary).to_be_visible()
                expect(summary).to_have_attribute("open", "")
                expect(summary.get_by_text("Timed records", exact=True)).to_be_visible()
                self.db(self._assert_originals_retained, originals)

    def test_location_assumptions_have_relevant_keyboard_accessible_controls(self):
        person = Person.objects.get(case=self.case)
        PersonLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic observation",
            person=person,
            latitude=0,
            longitude=0,
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                for operation in (
                    "location_quality",
                    "travel_discrepancies",
                    "location_conflicts",
                ):
                    self._choose_analysis(page, operation)
                    label = (
                        "Claimed event being compared"
                        if operation == "location_conflicts"
                        else "Coordinate discrepancy threshold (metres)"
                    )
                    field = page.get_by_label(label, exact=True)
                    field.focus()
                    expect(field).to_be_focused()
                    self.assertNotEqual(
                        field.evaluate("node => getComputedStyle(node).outlineStyle"),
                        "none",
                    )
                    field.fill(
                        "Synthetic delivery"
                        if operation == "location_conflicts"
                        else "250"
                    )
                    if operation != "travel_discrepancies":
                        expect(page.locator('[name="gap_minutes"]')).to_have_count(0)
                    if operation == "location_conflicts":
                        expect(page.locator('[name="distance_m"]')).to_have_count(0)
                        expect(
                            page.locator('[name="time_window_minutes"]')
                        ).to_have_count(0)
                    for width, height, label in (
                        (1440, 900, "desktop"),
                        (1440, 1024, "tall-desktop"),
                        (768, 1024, "tablet"),
                        (390, 844, "mobile"),
                        (320, 844, "reflow"),
                    ):
                        page.set_viewport_size({"width": width, "height": height})
                        self._assert_reflow(page)
                        field.scroll_into_view_if_needed()
                        self.capture(page, engine, f"assumptions-{operation}-{label}")

    def test_required_analysis_context_is_visible_without_javascript(self):
        point = PersonLocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Synthetic area observation",
            person=Person.objects.get(case=self.case),
            latitude=0,
            longitude=0,
        )
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page)
                base_url = self.url("core:analysis:create", case_id=self.case.pk)
                for operation, label in (
                    ("proximity", "Area centre latitude"),
                    ("style_change", "First writing sample"),
                    ("location_conflicts", "Claimed event being compared"),
                ):
                    page.goto(base_url + f"?operation={operation}")
                    field = page.get_by_label(label, exact=True)
                    expect(field).to_be_visible()
                    expect(page.locator(".analysis-assumptions[open]")).to_have_count(0)
                    field.focus()
                    expect(field).to_be_focused()
                    page.set_viewport_size({"width": 390, "height": 844})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"task-context-{operation}-mobile")
                source = page.get_by_label(
                    f"Person location · {point.title}", exact=True
                )
                source.check()
                page.get_by_role("button", name="Start analysis", exact=True).click()
                expect(page.locator("[data-error-summary]")).to_be_visible()
                expect(source).to_be_checked()
                expect(
                    page.get_by_label("Claimed event being compared", exact=True)
                ).to_be_visible()
                self.assertEqual(
                    self.db(AnalysisRun.objects.filter(case=self.case).count), 0
                )

    def test_complete_lists_and_reports_paginate_without_javascript(self):
        person = Person.objects.create(
            case=self.case, first_name="Synthetic correspondent"
        )
        for number in range(30):
            PhoneCallEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=person,
                recipient=person,
                title=f"Paginated call {number:02}",
                duration=timedelta(seconds=number),
            )
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Paginated source {number:02}",
                body="Synthetic source wording.",
            )
        messages = []
        for number in range(105):
            message = TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=person,
                recipient=person,
                title=f"Thread message {number + 1:03}",
                body=f"Synthetic retained message {number + 1}.",
            )
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=self.document,
                source_sha256=self.document.sha256,
                source_row=number + 1,
                source_start=number,
                source_end=number + 1,
                parser_version="synthetic.v1",
                timezone="UTC",
                raw_sender="Synthetic correspondent",
                native_thread_id="synthetic-thread",
            )
            messages.append(message)
        for engine in self.engines:
            with (
                self.subTest(engine=engine),
                self.browser_page(engine, java_script_enabled=False) as (_, page),
            ):
                self.sign_in(page, self.owner)
                page.goto(
                    self.live_server_url
                    + reverse("core:analysis:report", args=(self.case.pk, "calls"))
                )
                expect(
                    page.get_by_role(
                        "heading", name=re.compile("31 selected call records")
                    )
                ).to_be_visible()
                next_records = page.get_by_role("link", name="Next records", exact=True)
                next_records.focus()
                next_records.press("Enter")
                expect(page.get_by_label("Report record pages")).to_contain_text(
                    "Page 2"
                )
                expect(
                    page.get_by_role(
                        "heading", name=re.compile("31 selected call records")
                    )
                ).to_be_visible()
                for width, height in (
                    (1440, 900),
                    (1440, 1024),
                    (768, 1024),
                    (390, 844),
                    (320, 900),
                ):
                    page.set_viewport_size({"width": width, "height": height})
                    self._assert_reflow(page)
                    self.capture(page, engine, f"complete-report-{width}-{height}")
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(
                    self.url("core:analysis:create", case_id=self.case.pk)
                    + "?operation=entities&q=Paginated+source"
                )
                selected = page.locator(
                    '.analysis-source-option input[type="checkbox"]'
                ).first
                selected_value = selected.get_attribute("value")
                selected.check()
                page.get_by_role("button", name="Next sources", exact=True).click()
                expect(page.get_by_label("Source choice pages")).to_contain_text(
                    "Page 2"
                )
                expect(
                    page.locator(
                        f'input[type="hidden"][name="sources"][value="{selected_value}"]'
                    )
                ).to_have_count(1)
                page.get_by_role("button", name="Previous sources", exact=True).click()
                expect(
                    page.locator(f'input[type="checkbox"][value="{selected_value}"]')
                ).to_be_checked()
                page.get_by_label("Find evidence by title").fill("No matching source")
                page.get_by_role("button", name="Find evidence", exact=True).click()
                expect(
                    page.get_by_role("heading", name="No suitable sources", exact=True)
                ).to_be_visible()
                page.get_by_role(
                    "button", name="Clear title filter", exact=True
                ).click()
                expect(
                    page.locator(f'input[type="checkbox"][value="{selected_value}"]')
                ).to_be_checked()
                self.capture(page, engine, "complete-source-picker")
                page.goto(
                    self.url(
                        "core:evidence:detail",
                        case_id=self.case.pk,
                        kind="text-message",
                        evidence_id=messages[-1].pk,
                    )
                    + "?info=1&thread_page=5"
                )
                expect(page.get_by_label("Native thread pages")).to_contain_text(
                    "Page 5 of 5"
                )
                expect(
                    page.get_by_role("link", name="Thread message 105", exact=True).last
                ).to_be_visible()
                self.capture(page, engine, "complete-native-thread")

from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.analysis.form_base import SOURCE_PAGE_SIZE
from core.analysis.sources import source_id
from core.analysis.testing import preview_image_bytes
from core.analysis.tests.test_execution import LOCAL
from core.analysis.worker import process_run
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from image.models import ImageEvidence
from phone.models import CallEvidenceAttachment, PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence


@override_settings(PANDECT_LOCAL_AI=LOCAL)
class ClaimComparisonViewTests(EvidenceViewTestMixin, TestCase):
    operation = "claim_comparison"
    claim_text = "Booking R41\r\nwas cancelled."
    response_quote = "Reservation R41 has been cancelled."

    def setUp(self):
        super().setUp()
        self.login("editor")
        self.create_url = reverse("core:analysis:create", args=[self.case.pk])
        self.confirm_url = reverse("core:analysis:claim_confirm", args=[self.case.pk])
        self.message = self.message_source("Original message")
        self.notice = TextEvidence.objects.create(
            case=self.case,
            title="Cancellation notice",
            body=f"Automatic confirmation. {self.response_quote}",
            uploaded_by=self.users["owner"],
        )
        self.selected = [f"text-message:{self.message.pk}", f"text:{self.notice.pk}"]

    def message_source(self, title):
        return TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title=title,
            sender=self.person,
            recipient=self.person,
            body=f"📌 In case you missed it: {self.claim_text}",
        )

    def choose(self, selections):
        return self.client.post(
            self.create_url,
            {
                "operation": self.operation,
                "sources": selections,
                "submission_id": str(uuid4()),
            },
        )

    def prepare(self, selections=None):
        response = self.choose(self.selected if selections is None else selections)
        self.assertContains(response, "Choose the claim")
        return response.context["draft"]

    def confirm(self, draft, **overrides):
        return self.client.post(
            self.confirm_url,
            {
                "draft": draft,
                "source_id": source_id({"kind": "text-message", "id": self.message.pk}),
                "quote": self.claim_text.replace("\r\n", "\n"),
                **overrides,
            },
        )

    def run_reply(self):
        return {
            "items": [
                {
                    "source_id": source_id({"kind": "text", "id": self.notice.pk}),
                    "status": "supporting",
                    "quote": self.response_quote,
                    "reason": "The notice reports cancellation for booking R41.",
                }
            ]
        }

    def test_exact_case_sources_render_with_citations_and_copies_remain_distinct(self):
        duplicate = self.message_source("Retained copy of original message")
        draft = self.prepare([*self.selected, f"text-message:{duplicate.pk}"])
        queued = self.confirm(draft)
        self.assertEqual(queued.status_code, 302)
        self.assertEqual(self.confirm(draft).url, queued.url)
        run = AnalysisRun.objects.get(operation=self.operation)
        self.assertFalse(run.external_processing)
        span = run.parameters["claim"]
        self.assertEqual(
            self.message.body[span["start"] : span["end"]], self.claim_text
        )

        with (
            patch(
                "core.analysis.local.LocalClient.infer", return_value=self.run_reply()
            ),
            patch("core.analysis.gateway.GatewayClient.infer") as gateway,
        ):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        assessment = next(
            item
            for item in run.result.data["items"]
            if item["label"] == "Potentially supporting"
        )
        anchor = next(
            anchor
            for anchor in assessment["anchors"]
            if anchor["source_id"] == source_id({"kind": "text", "id": self.notice.pk})
        )
        self.assertEqual(
            self.notice.body[anchor["start"] : anchor["end"]], self.response_quote
        )
        gateway.assert_not_called()
        self.login("viewer")
        reading = self.client.get(queued.url)
        self.assertContains(reading, "Potentially supporting")
        self.assertContains(reading, self.response_quote)
        self.assertContains(reading, "AI suggestion")
        self.assertContains(reading, "Exact copy of claim")
        self.assertContains(reading, f'href="{self.url("detail", self.notice)}"')
        self.assertContains(
            reading, f'href="{self.url("detail", duplicate, kind="text-message")}"'
        )
        self.login("outsider")
        self.assertEqual(self.client.get(queued.url).status_code, 404)

    def test_selection_and_confirmation_recheck_case_access_and_original_availability(
        self,
    ):
        foreign = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Other case only",
            body="Unrelated protected wording.",
        )
        denied = self.choose([*self.selected, f"text:{foreign.pk}"])
        self.assertNotIn(foreign.body, denied.content.decode())
        self.assertFalse(AnalysisRun.objects.filter(operation=self.operation).exists())

        with patch(
            "text.claim_comparison_requests.snapshot_and_resolve_sources",
            side_effect=Http404,
        ):
            changed = self.choose(self.selected)
        self.assertContains(changed, "A selected original or exact extraction")
        self.assertEqual(changed.context["form"]["sources"].value(), self.selected)

        draft = self.prepare()
        rejected = self.confirm(draft, quote="Invented wording.")
        self.assertContains(rejected, "Copy the exact wording")
        self.assertContains(rejected, "Invented wording.")
        self.login("viewer")
        self.assertEqual(self.confirm(draft).status_code, 403)
        self.login("owner")
        self.assertEqual(self.confirm(draft).status_code, 404)
        self.login("editor")
        self.client.post(self.url("archive", self.notice), {"action": "archive"})
        self.notice.refresh_from_db()
        self.assertTrue(self.notice.is_archived)
        stale = self.confirm(draft)
        self.assertNotContains(stale, self.notice.body, status_code=400)
        self.assertEqual(
            stale.context["picker_url"],
            self.create_url + "?operation=claim_comparison",
        )
        self.assertContains(stale, "Back to evidence selection", status_code=400)
        self.notice.delete()
        back = self.client.post(
            stale.context["picker_url"],
            {
                "operation": self.operation,
                "submission_id": stale.context["submission_id"],
                "sources": stale.context["tokens"],
                "browse_sources": "review",
            }
            | stale.context["picker_state"],
        )
        self.assertContains(
            back, "Unavailable sources were cleared from the selection."
        )
        self.assertTrue(
            any(
                option.data["value"] == self.selected[0] and option.data["selected"]
                for option, _, _ in back.context["source_options"]
            )
        )
        self.assertNotContains(back, f'value="{self.selected[1]}"')
        retried = self.client.post(
            stale.context["picker_url"],
            {
                "operation": self.operation,
                "submission_id": stale.context["submission_id"],
                "sources": [self.selected[0], f"text:{self.text().pk}"],
                "sources_cursor": self.selected[1],
            },
        )
        self.assertContains(retried, "Choose the claim")
        self.assertEqual(retried.context["picker_state"]["sources_cursor"], "")
        self.assertFalse(AnalysisRun.objects.filter(operation=self.operation).exists())

    def test_broadening_a_blocked_reader_keeps_the_return_to_that_record(self):
        source = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Scanned booking confirmation without OCR",
            file=SimpleUploadedFile("booking.png", preview_image_bytes()),
        )
        filters = {"q": "booking confirmation", "type": "image", "page": "2"}
        method_url = reverse(
            "core:analysis:create",
            args=[self.case.pk],
            query={
                "operation": self.operation,
                "source": f"image:{source.pk}",
                "return_collection": urlencode(filters),
            },
        )
        reader = reverse(
            "core:evidence:detail",
            args=[self.case.pk, "image", source.pk],
            query=filters,
            fragment="evidence-reader",
        )
        fixed = self.client.get(method_url)
        self.assertTrue(fixed.context["fixed_source"])
        picker = fixed.context["case_sources_url"]
        self.assertContains(
            fixed, f'href="{escape(picker)}">Choose from all case evidence</a>'
        )
        expanded = self.client.get(picker)
        self.assertFalse(expanded.context["fixed_source"])
        prepared = self.client.post(
            picker,
            {
                "operation": self.operation,
                "sources": self.selected,
                "submission_id": str(uuid4()),
            },
        )
        self.assertContains(prepared, f'href="{escape(reader)}">Cancel</a>')
        queued = self.confirm(prepared.context["draft"])
        self.assertEqual(queued.status_code, 302)
        result = self.client.get(queued.url)
        self.assertContains(result, f'href="{escape(reader)}">Back to evidence</a>')

    def test_reader_and_paged_source_selection_survive_comparison(self):
        for index in range(SOURCE_PAGE_SIZE):
            TextEvidence.objects.create(
                case=self.case,
                title=f"Cancellation record {index}",
                body="This retained copy concerns a different reservation.",
                uploaded_by=self.users["owner"],
            )
        reader_query = {"q": "Booking & café", "type": "text-message", "page": "2"}
        method_url = reverse(
            "core:analysis:create",
            args=[self.case.pk],
            query={
                "operation": self.operation,
                "source": self.selected[0],
                "return_collection": urlencode(reader_query),
            },
        )
        reader = reverse(
            "core:evidence:detail",
            args=[self.case.pk, "text-message", self.message.pk],
            query=reader_query,
            fragment="evidence-reader",
        )
        first = self.client.get(f"{method_url}&q=Cancellation")
        choices = first.context["form"].source_page
        self.assertTrue(choices.has_next())
        selection = {
            "operation": self.operation,
            "sources": self.selected,
            "submission_id": str(uuid4()),
            "q": "Cancellation",
            "sources_page": "2",
            "sources_cursor": choices.after,
        }
        prepared = self.client.post(method_url, selection)
        self.assertContains(prepared, "Choose the claim")
        self.assertContains(prepared, f'href="{escape(reader)}">Cancel</a>')
        draft = prepared.context["draft"]

        invalid = self.confirm(draft, quote="Missing from the retained message.")
        self.assertContains(invalid, "Copy the exact wording")
        self.assertContains(invalid, f'href="{escape(reader)}">Cancel</a>')
        self.assertContains(
            invalid, f'action="{escape(invalid.context["picker_url"])}"'
        )
        self.assertContains(invalid, 'name="q" value="Cancellation"')
        back = self.client.post(
            invalid.context["picker_url"],
            selection | invalid.context["picker_state"] | {"browse_sources": "review"},
        )
        form = back.context["form"]
        self.assertEqual(form.source_page.number, 2)
        self.assertEqual(form.sources_cursor, choices.after)
        self.assertEqual(back.context["query"], "Cancellation")
        self.assertEqual(form["sources"].value(), self.selected)
        self.assertFalse(AnalysisRun.objects.exists())

        queued = self.confirm(draft, return_evidence="ignored")
        self.assertEqual(queued.status_code, 302)
        self.assertEqual(self.confirm(draft).url, queued.url)
        result = self.client.get(queued.url)
        self.assertContains(result, f'href="{escape(reader)}">Back to evidence</a>')
        self.assertEqual(
            self.client.get(result.context["back_to_analysis"]).context["item"],
            self.message,
        )
        self.assertEqual(
            AnalysisRun.objects.filter(operation=self.operation).count(), 1
        )

    def test_deleted_reader_returns_to_filtered_evidence_without_blocking_comparison(
        self,
    ):
        alternate = self.message_source("Retained statement")
        reader_query = {"q": "Booking & café", "type": "text-message", "page": "2"}
        method_url = reverse(
            "core:analysis:create",
            args=[self.case.pk],
            query={
                "operation": self.operation,
                "source": self.selected[0],
                "return_collection": urlencode(reader_query),
            },
        )
        eligible = [f"text-message:{alternate.pk}", self.selected[1]]
        prepared = self.client.post(
            method_url,
            {
                "operation": self.operation,
                "sources": eligible,
                "submission_id": str(uuid4()),
            },
        )
        self.assertContains(prepared, "Choose the claim")
        draft = prepared.context["draft"]
        self.message.delete()
        collection = reverse(
            "core:evidence:list", args=[self.case.pk], query=reader_query
        )
        alternate_source = source_id({"kind": "text-message", "id": alternate.pk})
        invalid = self.confirm(draft, source_id=alternate_source, quote="Incorrect")
        self.assertContains(invalid, f'href="{escape(collection)}">Cancel</a>')
        selection = {
            "operation": self.operation,
            "submission_id": invalid.context["submission_id"],
            "sources": invalid.context["tokens"],
        } | invalid.context["picker_state"]
        back = self.client.post(
            invalid.context["picker_url"],
            selection | {"browse_sources": "review"},
        )
        self.assertEqual(back.context["form"]["sources"].value(), eligible)
        reselected = self.client.post(invalid.context["picker_url"], selection)
        self.assertContains(reselected, f'href="{escape(collection)}">Cancel</a>')
        queued = self.confirm(reselected.context["draft"], source_id=alternate_source)
        self.assertEqual(queued.status_code, 302)
        self.assertEqual(
            self.confirm(draft, source_id=alternate_source).url, queued.url
        )
        result = self.client.get(queued.url)
        self.assertContains(result, f'href="{escape(collection)}">Back to evidence</a>')

    def test_selection_requires_a_message_or_call_transcript(self):
        other = self.text()
        response = self.choose([self.selected[1], f"text:{other.pk}"])
        self.assertContains(response, "Include a retained message or call transcript.")
        self.assertNotContains(response, "Choose the claim")

    def test_revoke_before_execution_withholds_all_material_from_inference(self):
        self.assertEqual(self.confirm(self.prepare()).status_code, 302)
        run = AnalysisRun.objects.get(operation=self.operation)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        with patch("core.analysis.local.LocalClient.infer") as infer:
            process_run(run.pk)
        infer.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(hasattr(run, "result"))

    def test_supplied_call_transcript_pins_the_exact_attachment(self):
        call = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic booking call",
            sender=self.person,
            recipient=self.person,
        )
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            transcript=f"Speaker 1: {self.claim_text}",
        )
        draft = self.prepare(
            [f"phone-call:{call.pk}:{attachment.pk}", self.selected[1]]
        )
        selected_claim = source_id(
            {"kind": "phone-call", "id": call.pk, "attachment_id": attachment.pk}
        )
        queued = self.confirm(draft, source_id=selected_claim)
        self.assertEqual(queued.status_code, 302)
        run = AnalysisRun.objects.get(operation=self.operation)
        self.assertEqual(run.parameters["claim"]["source_id"], selected_claim)
        snapshot = next(
            source for source in run.inputs if source["id"] == selected_claim
        )
        self.assertEqual(snapshot["attachment_id"], str(attachment.pk))
        self.assertEqual(snapshot["sha256"], attachment.sha256)

        with patch(
            "core.analysis.local.LocalClient.infer", return_value=self.run_reply()
        ):
            process_run(run.pk)
        reading = self.client.get(queued.url)
        source_url = self.url("detail", call, kind="phone-call")
        self.assertContains(reading, f'href="{source_url}#attachment-{attachment.pk}"')

    def test_retry_retains_exact_inputs_after_private_service_failure(self):
        self.assertEqual(self.confirm(self.prepare()).status_code, 302)
        original = AnalysisRun.objects.get(operation=self.operation)
        with patch(
            "core.analysis.local.LocalClient.infer",
            side_effect=AnalysisError("local_ai_unavailable"),
        ):
            process_run(original.pk)
        original.refresh_from_db()
        self.assertEqual(original.status, "failed")
        self.assertFalse(hasattr(original, "result"))

        retry_url = reverse("core:analysis:retry", args=[self.case.pk, original.pk])
        self.assertEqual(self.client.post(retry_url).status_code, 302)
        retry = AnalysisRun.objects.get(previous_run=original)
        self.assertEqual(retry.inputs, original.inputs)
        self.assertEqual(retry.parameters, original.parameters)
        with patch(
            "core.analysis.local.LocalClient.infer", return_value=self.run_reply()
        ):
            process_run(retry.pk)
        retry.refresh_from_db()
        self.assertEqual(retry.status, "complete", retry.error_code)

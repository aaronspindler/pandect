"""Reviewed source intake never creates people or messages before publication."""

import csv
import io
from copy import copy, deepcopy
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.analysis.import_checks import imported_sources
from core.analysis.services import process_run, resolve_inputs
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.imessage import HEADERS, iter_import_rows
from phone.imessage_forms import IMessageConfirmForm, IMessageUploadForm
from phone.imessage_imports import confirm_import, start_preview
from phone.models import CommunicationContext, TextMessageEvidence

SYNTHETIC_MESSAGE_GATEWAY = {
    "account": "a" * 32,
    "gateway": "synthetic-message-analysis",
    "gateway_token": "synthetic-gateway-secret",
    "workers_token": "synthetic-workers-secret",
    "text_provider": "workers-ai",
    "models": {"text": "@cf/test/text-model"},
    "timeout": 8,
    "max_request_bytes": 100_000,
    "max_response_bytes": 100_000,
}
SYNTHETIC_MESSAGE_LOCAL = {
    "text": {
        "url": "http://private-text.test:11434",
        "model": "qwen3.5:9b",
        "sha256": "a" * 64,
        "runtime": "ollama@0.40.11",
        "context_tokens": 32768,
    },
    "timeout": 120,
    "max_request_bytes": 8388608,
    "max_response_bytes": 2097152,
    "max_output_tokens": 4096,
}


def message_csv(*rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=HEADERS)
    writer.writeheader()
    for index, values in enumerate(
        rows or ({}, {"Type": "Outgoing", "Sender ID": "", "Sender Name": ""})
    ):
        writer.writerow(
            dict.fromkeys(HEADERS, "")
            | {
                "Chat Session": "Synthetic correspondent",
                "Message ID": f"message-{index}",
                "Message Date": "2026-07-29 21:23:10",
                "Service": "iMessage",
                "Type": "Incoming",
                "Sender ID": "17055550100",
                "Sender Name": "Synthetic correspondent",
                "Text": "  Original café\r\nmessage.  ",
            }
            | values
        )
    return stream.getvalue().encode("utf-8")


class IMessageImportViewTests(EvidenceViewTestMixin, TestCase):
    def upload_url(self, case=None):
        return reverse("phone:imessage_upload", args=[(case or self.case).pk])

    def review_url(self, run, case=None):
        return reverse("phone:imessage_review", args=[(case or self.case).pk, run.pk])

    def prepare(self, content=None, **parameters):
        self.login("owner")
        content = message_csv() if content is None else content
        response = self.client.post(
            self.upload_url(),
            {
                "submission_id": str(uuid4()),
                "timezone": "America/Toronto",
                "confirm_timezone": "on",
                "file": SimpleUploadedFile(
                    "messages.csv", content, content_type="text/csv"
                ),
                **parameters,
            },
        )
        self.assertEqual(response.status_code, 302)
        run = AnalysisRun.objects.get(
            operation="imessage_import", parameters__phase="preview"
        )
        process_run(run.pk)
        run.refresh_from_db()
        return run

    def analysis_selection(self, preview=None):
        preview = preview or AnalysisRun.objects.filter(
            operation="imessage_import", parameters__phase="preview"
        ).latest("created_at")
        actor = next(
            user
            for user in self.users.values()
            if str(user.pk) == self.client.session["_auth_user_id"]
        )
        form = IMessageConfirmForm(actor=actor, case=self.case, run=preview)
        return {
            "mode": "local",
            "execution_confirmation": form["execution_confirmation"].value(),
        }

    def new_people(self):
        return {
            "account-person": "create",
            "account-first_name": "Export account",
            "correspondent-person": "create",
            "correspondent-first_name": "Correspondent",
            "correspondent-phone_number": "17055550100",
            "direct_chat": "on",
            "confirm": "on",
        } | self.analysis_selection()

    def test_upload_retains_exact_csv_and_retries_without_duplicates(self):
        self.login("owner")
        raw = message_csv()
        submission = str(uuid4())

        def payload(content=raw):
            return {
                "submission_id": submission,
                "timezone": "America/Toronto",
                "confirm_timezone": "on",
                "file": SimpleUploadedFile(
                    "messages.csv", content, content_type="text/csv"
                ),
            }

        before = Person.objects.count()
        first = self.client.post(self.upload_url(), payload())
        second = self.client.post(self.upload_url(), payload())
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.url, first.url)
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        document = DocumentEvidence.objects.get()
        with document.file.open("rb") as retained:
            self.assertEqual(retained.read(), raw)
        self.assertEqual(Person.objects.count(), before)
        self.assertFalse(TextMessageEvidence.objects.exists())
        conflict = self.client.post(
            self.upload_url(), payload(message_csv({"Text": "Changed"}))
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(DocumentEvidence.objects.count(), 1)

    def test_csv_import_streaming_limit_does_not_reduce_ordinary_uploads(self):
        self.login("owner")
        raw = message_csv()
        with (
            patch("phone.analysis.deterministic.imessage.MAX_FILE_BYTES", len(raw) - 1),
            self.settings(PANDECT_MAX_FILE_BYTES=2 * len(raw)),
        ):
            page = self.client.get(self.upload_url())
            self.assertEqual(page.context["max_bytes"], len(raw) - 1)
            rejected = self.client.post(
                self.upload_url(),
                {
                    "submission_id": str(uuid4()),
                    "timezone": "UTC",
                    "confirm_timezone": "on",
                    "file": SimpleUploadedFile("messages.csv", raw),
                },
            )
            self.assertEqual(rejected.status_code, 400)
            self.assertTrue(rejected.wsgi_request.evidence_upload_too_large)
            self.assertFalse(DocumentEvidence.objects.exists())
            self.assertFalse(AnalysisRun.objects.exists())
            self.assertFalse(list(self.media_root.rglob("*")))
            accepted = self.client.post(
                self.url("create"),
                self.payload("document", file=SimpleUploadedFile("messages.csv", raw)),
                HTTP_ACCEPT="application/json",
            )
            self.assertEqual(accepted.status_code, 201)
        self.assertEqual(DocumentEvidence.objects.get().byte_size, len(raw))

    def test_intake_form_rejects_large_uploaded_and_retained_csv(self):
        raw = message_csv()
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained CSV",
            file=SimpleUploadedFile("messages.csv", raw),
        )
        with patch(
            "phone.analysis.deterministic.imessage.MAX_FILE_BYTES", len(raw) - 1
        ):
            for field, value in (
                ("file", SimpleUploadedFile("messages.csv", raw)),
                ("document", str(document.pk)),
            ):
                with self.subTest(field=field):
                    form = IMessageUploadForm(
                        {
                            "submission_id": str(uuid4()),
                            "timezone": "UTC",
                            "confirm_timezone": "on",
                            **({field: value} if field == "document" else {}),
                        },
                        {field: value} if field == "file" else None,
                        case=self.case,
                    )
                    self.assertFalse(form.is_valid())
                    self.assertIn(field, form.errors)

    def test_intake_service_rejects_large_sources_before_hashing_or_queueing(self):
        raw = message_csv()
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained CSV",
            file=SimpleUploadedFile("messages.csv", raw),
        )
        with (
            patch("phone.analysis.deterministic.imessage.MAX_FILE_BYTES", len(raw) - 1),
            patch("phone.imessage_imports.file_integrity") as integrity,
            patch("phone.imessage_imports.start_run") as queue,
        ):
            for source in (
                {"file": SimpleUploadedFile("messages.csv", raw)},
                {"document": document},
            ):
                with self.subTest(source=next(iter(source))):
                    with self.assertRaises(AnalysisError) as raised:
                        start_preview(
                            self.users["owner"],
                            self.case.pk,
                            source | {"submission_id": uuid4(), "timezone": "UTC"},
                        )
                    self.assertEqual(raised.exception.code, "imessage_import_limit")
            integrity.assert_not_called()
            queue.assert_not_called()
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertFalse(AnalysisRun.objects.exists())

    def test_review_and_confirm_create_people_only_in_worker_and_bind_choices(self):
        preview = self.prepare()
        self.assertEqual(preview.status, "complete", preview.error_code)
        before = Person.objects.count()
        response = self.client.get(self.review_url(preview))
        self.assertContains(response, "2 ready")
        self.assertContains(response, "17055550100")
        self.assertContains(response, "Message preview")
        self.assertContains(response, "Retained excerpts from the first 2 source rows")
        self.assertLess(
            response.content.index(b'id="imessage-preview-heading"'),
            response.content.index(b'name="account-person"'),
        )
        self.assertFalse(TextMessageEvidence.objects.exists())
        first = self.client.post(self.review_url(preview), self.new_people())
        second = self.client.post(self.review_url(preview), self.new_people())
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.url, first.url)
        self.assertEqual(Person.objects.count(), before)
        final = AnalysisRun.objects.get(parameters__phase="import")
        self.assertEqual(final.previous_run_id, preview.pk)
        self.assertEqual(final.parameters["analysis"]["mode"], "local")
        self.assertEqual(final.parameters["analysis"]["executions"], {})
        self.assertFalse(final.parameters["analysis"]["external_processing"])
        changed = self.client.post(
            self.review_url(preview),
            self.new_people() | {"account-first_name": "Changed"},
        )
        self.assertEqual(changed.status_code, 409)
        self.assertContains(changed, "already used", status_code=409)
        with self.settings(PANDECT_LOCAL_AI=SYNTHETIC_MESSAGE_LOCAL):
            changed_mode = self.client.post(
                self.review_url(preview),
                self.new_people() | {"mode": "ai"},
            )
        self.assertContains(changed_mode, "already used", status_code=409)
        final.refresh_from_db()
        self.assertEqual(final.parameters["analysis"]["mode"], "local")
        self.assertEqual(final.parameters["analysis"]["executions"], {})
        self.assertEqual(
            AnalysisRun.objects.filter(parameters__phase="import").count(), 1
        )
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "complete", final.error_code)
        self.assertEqual(Person.objects.count(), before + 2)
        self.assertEqual(TextMessageEvidence.objects.count(), 2)
        self.assertEqual(CommunicationContext.objects.count(), 2)
        self.assertContains(
            self.client.get(self.review_url(final)), "Messages imported"
        )

    def test_review_defaults_to_local_without_queueing_analysis(self):
        preview = self.prepare()
        response = self.client.get(self.review_url(preview))
        form = response.context["form"]
        self.assertEqual(form["mode"].value(), "local")
        self.assertTrue(form["execution_confirmation"].value())
        self.assertContains(response, "Analysis after import")
        self.assertContains(response, "Local checks")
        self.assertNotContains(response, 'name="external_processing"')
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    def test_date_limited_review_only_publishes_and_analyzes_selected_messages(self):
        bounds = {"start_date": "2026-07-29", "end_date": "2026-07-29"}
        raw = message_csv(
            {"Message Date": "2026-07-28 23:59:59", "Text": "Before the range"},
            {"Text": "Selected message"},
            {"Message Date": "2026-07-30 00:00:00", "Text": "After the range"},
        )
        preview = self.prepare(raw, **bounds)
        self.assertEqual(preview.status, "complete", preview.error_code)
        review = self.client.get(self.review_url(preview), {"page": 1})
        self.assertContains(review, "2026-07-29 to 2026-07-29")
        self.assertContains(review, "2 outside date range")
        self.assertContains(review, "Before the range")
        excluded = self.client.get(self.review_url(preview), {"rows": "excluded"})
        self.assertContains(excluded, "Excluded message rows")
        self.assertNotContains(excluded, "Selected message")
        self.assertEqual(
            [item["values"]["source_row"] for item in excluded.context["rows"]],
            [2, 4],
        )
        confirmation = self.client.post(
            self.review_url(preview),
            self.new_people() | {"start_date": "2026-07-28", "end_date": "2026-07-30"},
        )
        final = AnalysisRun.objects.get(parameters__phase="import")
        self.assertRedirects(confirmation, self.review_url(final))
        self.assertEqual({field: final.parameters[field] for field in bounds}, bounds)
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "complete", final.error_code)
        message = TextMessageEvidence.objects.get()
        self.assertEqual(message.body, "Selected message")
        self.assertEqual(
            CommunicationContext.objects.get(message=message).source_row, 3
        )
        _, sources = imported_sources(self.case, CaseAnalysisBatch.objects.get().scope)
        self.assertEqual(list(sources.values_list("pk", flat=True)), [message.pk])
        with DocumentEvidence.objects.get().file.open("rb") as retained:
            self.assertEqual(retained.read(), raw)

    def test_reversed_date_range_is_rejected_before_retaining_the_file(self):
        self.login("owner")
        response = self.client.post(
            self.upload_url(),
            {
                "submission_id": str(uuid4()),
                "timezone": "America/Toronto",
                "confirm_timezone": "on",
                "start_date": "2026-07-30",
                "end_date": "2026-07-29",
                "file": SimpleUploadedFile("messages.csv", message_csv()),
            },
        )
        self.assertContains(
            response,
            "The end date must be on or after the start date.",
            status_code=400,
        )
        self.assertEqual(response.context["form"]["start_date"].value(), "2026-07-30")
        self.assertEqual(response.context["form"]["end_date"].value(), "2026-07-29")
        self.assertFalse(DocumentEvidence.objects.exists())
        self.assertFalse(AnalysisRun.objects.exists())

    def test_completed_import_opens_the_currently_published_message(self):
        earlier = self.prepare(
            message_csv(
                {"Text": "Earlier publication"},
                {"Message Date": "2026-07-30 08:00:00", "Text": "New publication"},
            ),
            end_date="2026-07-29",
        )
        document = DocumentEvidence.objects.get()
        other = Person.objects.create(case=self.case, first_name="Correspondent")
        owner = self.users["owner"]
        people = {
            "account": {"id": str(self.person.pk), "create": None},
            "correspondent": {"id": str(other.pk), "create": None},
        }
        final, _ = confirm_import(owner, self.case.pk, earlier.pk, people)
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "complete", final.error_code)

        later, _ = start_preview(
            owner,
            self.case.pk,
            {
                "submission_id": uuid4(),
                "document": document,
                "timezone": "America/Toronto",
                "start_date": date(2026, 7, 30),
            },
        )
        process_run(later.pk)
        later.refresh_from_db()
        self.assertEqual(later.status, "complete", later.error_code)
        final, _ = confirm_import(owner, self.case.pk, later.pk, people)
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "complete", final.error_code)
        message = TextMessageEvidence.objects.get(body="New publication")
        receipt = self.client.get(self.review_url(final))
        self.assertContains(receipt, f"?focus={message.pk}#message-{message.pk}")
        self.assertContains(receipt, "1 new message added")

    def test_reused_import_opens_message_within_reviewed_export_dates(self):
        preview = self.prepare(
            message_csv(
                {"Message Date": "2026-07-30 01:00:00+00:00", "Text": "Earlier"},
                {"Message Date": "2026-07-31 03:30:00+00:00", "Text": "Selected"},
                {"Message Date": "2026-07-31 04:00:00+00:00", "Text": "Later"},
            )
        )
        document = DocumentEvidence.objects.get()
        owner = self.users["owner"]
        other = Person.objects.create(case=self.case, first_name="Correspondent")
        people = {
            "account": {"id": str(self.person.pk), "create": None},
            "correspondent": {"id": str(other.pk), "create": None},
        }
        final, _ = confirm_import(owner, self.case.pk, preview.pk, people)
        process_run(final.pk)
        self.assertEqual(TextMessageEvidence.objects.count(), 3)

        limited, _ = start_preview(
            owner,
            self.case.pk,
            {
                "submission_id": uuid4(),
                "document": document,
                "timezone": "America/Toronto",
                "start_date": date(2026, 7, 30),
                "end_date": date(2026, 7, 30),
            },
        )
        process_run(limited.pk)
        final, _ = confirm_import(owner, self.case.pk, limited.pk, people)
        process_run(final.pk)
        receipt = self.client.get(self.review_url(final))
        message = TextMessageEvidence.objects.get(body="Selected")
        self.assertContains(receipt, f"?focus={message.pk}#message-{message.pk}")
        self.assertContains(receipt, "0 new messages added · 1 already retained")
        self.assertEqual(TextMessageEvidence.objects.count(), 3)

    def test_retained_csv_with_no_matching_dates_cannot_be_confirmed(self):
        self.login("owner")
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained CSV",
            file=SimpleUploadedFile("messages.csv", message_csv()),
        )
        response = self.client.post(
            self.upload_url(),
            {
                "submission_id": str(uuid4()),
                "timezone": "America/Toronto",
                "confirm_timezone": "on",
                "document": str(document.pk),
                "end_date": "2026-07-28",
            },
        )
        preview = AnalysisRun.objects.get(parameters__phase="preview")
        self.assertRedirects(response, self.review_url(preview))
        process_run(preview.pk)
        review = self.client.get(self.review_url(preview))
        self.assertContains(review, "Through 2026-07-28")
        self.assertContains(review, "No messages are ready to import for these dates.")
        self.assertContains(review, f"?document={document.pk}")
        self.assertFalse(review.context["can_confirm"])
        confirmation = self.client.post(self.review_url(preview), self.new_people())
        self.assertEqual(confirmation.status_code, 409)
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    @override_settings(PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY)
    def test_external_ai_and_both_require_approval_and_preserve_draft(self):
        preview = self.prepare()
        before = Person.objects.count()
        for mode in ("ai", "both"):
            with self.subTest(mode=mode):
                response = self.client.post(
                    self.review_url(preview), self.new_people() | {"mode": mode}
                )
                self.assertEqual(response.status_code, 400)
                form = response.context["form"]
                self.assertIn("external_processing", form.errors)
                self.assertEqual(form["mode"].value(), mode)
                self.assertEqual(
                    response.context["account_form"]["first_name"].value(),
                    "Export account",
                )
                self.assertEqual(Person.objects.count(), before)
                self.assertFalse(
                    AnalysisRun.objects.filter(parameters__phase="import").exists()
                )
        approved = self.client.post(
            self.review_url(preview),
            self.new_people() | {"mode": "both", "external_processing": "on"},
        )
        self.assertEqual(approved.status_code, 302)
        final = AnalysisRun.objects.get(parameters__phase="import")
        self.assertEqual(final.parameters["analysis"]["mode"], "both")
        self.assertTrue(final.parameters["analysis"]["external_processing"])
        self.assertEqual(
            {
                route["backend"]
                for route in final.parameters["analysis"]["executions"].values()
            },
            {"gateway"},
        )
        self.assertEqual(len(final.parameters["analysis"]["executions"]), 4)
        self.assertFalse(TextMessageEvidence.objects.exists())
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "complete", final.error_code)
        response = self.client.get(self.review_url(final))
        self.assertContains(response, "Selected analysis: Local checks and AI")
        self.assertEqual(CaseAnalysisBatch.objects.get().scope["mode"], "both")

    @override_settings(
        PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY | {"account": ""},
        PANDECT_LOCAL_AI=SYNTHETIC_MESSAGE_LOCAL,
    )
    def test_missing_external_configuration_uses_local_ai_without_approval(self):
        preview = self.prepare()
        response = self.client.get(self.review_url(preview))
        self.assertContains(response, "AI will use the configured private service.")
        self.assertNotContains(response, 'name="external_processing"')
        for mode in ("ai", "both"):
            with self.subTest(mode=mode):
                form = IMessageConfirmForm(
                    self.new_people() | {"mode": mode},
                    actor=self.users["owner"],
                    case=self.case,
                    run=preview,
                )
                self.assertTrue(form.is_valid(), form.errors)
                analysis = form.specification()
                self.assertFalse(analysis["external_processing"])
                self.assertEqual(len(analysis["executions"]), 4)
                self.assertEqual(
                    {route["backend"] for route in analysis["executions"].values()},
                    {"local"},
                )
        response = self.client.post(
            self.review_url(preview), self.new_people() | {"mode": "ai"}
        )
        self.assertEqual(response.status_code, 302)
        final = AnalysisRun.objects.get(parameters__phase="import")
        self.assertEqual(final.parameters["analysis"]["mode"], "ai")
        self.assertFalse(final.parameters["analysis"]["external_processing"])
        self.assertTrue(
            all(
                route["backend"] == "local"
                for route in final.parameters["analysis"]["executions"].values()
            )
        )

    @override_settings(PANDECT_LOCAL_AI={})
    def test_unconfigured_ai_is_unavailable_and_local_checks_ignore_route_drift(self):
        preview = self.prepare()
        response = self.client.get(self.review_url(preview))
        self.assertContains(response, "AI analysis is unavailable")
        self.assertEqual(
            list(response.context["form"].fields["mode"].choices),
            [("local", "Local checks")],
        )
        payload = self.new_people()
        self.assertEqual(
            self.client.post(
                self.review_url(preview), payload | {"mode": "ai"}
            ).status_code,
            400,
        )
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )
        with self.settings(PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY):
            self.assertEqual(
                self.client.post(self.review_url(preview), payload).status_code, 302
            )
        self.assertEqual(
            AnalysisRun.objects.get(parameters__phase="import").parameters["analysis"][
                "executions"
            ],
            {},
        )

    def test_invalid_or_missing_analysis_mode_cannot_queue_import(self):
        preview = self.prepare()
        for mode in ("unreviewed-service", "external", ""):
            with self.subTest(mode=mode):
                response = self.client.post(
                    self.review_url(preview), self.new_people() | {"mode": mode}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("mode", response.context["form"].errors)
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    @override_settings(PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY)
    def test_analysis_confirmation_cannot_be_tampered_or_reused_by_another_editor(self):
        preview = self.prepare()
        original = self.new_people() | {
            "mode": "ai",
            "external_processing": "on",
        }
        tampered = self.client.post(
            self.review_url(preview),
            original | {"execution_confirmation": "invalid-token"},
        )
        self.assertEqual(tampered.status_code, 400)
        self.assertTrue(tampered.context["form"].non_field_errors())
        self.assertFalse(tampered.context["form"]["external_processing"].value())
        self.assertEqual(tampered.context["form"]["mode"].value(), "ai")
        self.login("editor")
        rebound = self.client.post(self.review_url(preview), original)
        self.assertEqual(rebound.status_code, 400)
        self.assertTrue(rebound.context["form"].non_field_errors())
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    def test_analysis_confirmation_is_bound_to_case_and_preview(self):
        preview = self.prepare()
        values = self.new_people()
        other_preview = copy(preview)
        other_preview.pk = uuid4()
        for case, run in ((self.other_case, preview), (self.case, other_preview)):
            with self.subTest(case=case.pk, run=run.pk):
                form = IMessageConfirmForm(
                    values, actor=self.users["owner"], case=case, run=run
                )
                self.assertFalse(form.is_valid())
                self.assertTrue(form.non_field_errors())
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    @override_settings(PANDECT_AI_GATEWAY=SYNTHETIC_MESSAGE_GATEWAY)
    def test_changed_analysis_destination_requires_fresh_approval(self):
        preview = self.prepare()
        original = self.new_people() | {
            "mode": "ai",
            "external_processing": "on",
        }
        with self.settings(
            PANDECT_AI_GATEWAY=settings.PANDECT_AI_GATEWAY
            | {"gateway": "changed-synthetic-gateway"}
        ):
            changed = self.client.post(self.review_url(preview), original)
            self.assertEqual(changed.status_code, 400)
            form = changed.context["form"]
            self.assertTrue(form.non_field_errors())
            self.assertEqual(form["mode"].value(), "ai")
            self.assertFalse(form["external_processing"].value())
            self.assertNotEqual(
                form["execution_confirmation"].value(),
                original["execution_confirmation"],
            )
            self.assertFalse(
                AnalysisRun.objects.filter(parameters__phase="import").exists()
            )
            refreshed = original | {
                "execution_confirmation": form["execution_confirmation"].value()
            }
            approved = self.client.post(self.review_url(preview), refreshed)
            self.assertEqual(approved.status_code, 302)
        final = AnalysisRun.objects.get(parameters__phase="import")
        self.assertEqual(final.parameters["analysis"]["mode"], "ai")

    def test_existing_people_and_rejected_cross_case_references(self):
        preview = self.prepare()
        other = Person.objects.create(case=self.case, first_name="Other participant")
        foreign = Person.objects.create(
            case=self.other_case, first_name="Foreign participant"
        )
        data = {
            "account-person": str(self.person.pk),
            "account-email": "unfinished draft",
            "correspondent-person": str(foreign.pk),
            "direct_chat": "on",
            "confirm": "on",
        } | self.analysis_selection(preview)
        invalid = self.client.post(self.review_url(preview), data)
        self.assertEqual(invalid.status_code, 400)
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )
        before = Person.objects.count()
        self.login("editor")
        good = self.client.post(
            self.review_url(preview),
            data
            | self.analysis_selection(preview)
            | {"correspondent-person": str(other.pk)},
        )
        self.assertEqual(good.status_code, 302)
        final = AnalysisRun.objects.get(parameters__phase="import")
        process_run(final.pk)
        self.assertEqual(Person.objects.count(), before)
        pairs = set(
            TextMessageEvidence.objects.values_list("sender_id", "recipient_id")
        )
        self.assertEqual(
            pairs, {(self.person.pk, other.pk), (other.pk, self.person.pk)}
        )

    def test_case_roles_viewer_read_and_revocation(self):
        preview = self.prepare()
        self.login("viewer")
        self.assertEqual(self.client.get(self.upload_url()).status_code, 403)
        page = self.client.get(self.review_url(preview))
        self.assertContains(page, "Viewer access")
        self.assertNotContains(page, 'name="account-person"')
        self.assertEqual(
            self.client.post(self.review_url(preview), self.new_people()).status_code,
            403,
        )
        self.login("outsider")
        self.assertEqual(self.client.get(self.review_url(preview)).status_code, 404)
        self.login("editor")
        self.assertEqual(self.client.get(self.review_url(preview)).status_code, 200)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(
            self.client.post(self.review_url(preview), self.new_people()).status_code,
            404,
        )
        self.login("owner")
        self.assertEqual(
            self.client.get(self.review_url(preview, case=self.other_case)).status_code,
            404,
        )

    def test_zero_ready_rows_cannot_create_people_or_confirm(self):
        preview = self.prepare(message_csv({"Text": "", "Attachment": "photo.jpg"}))
        before = Person.objects.count()
        page = self.client.get(self.review_url(preview))
        self.assertNotContains(page, 'name="account-person"')
        self.assertEqual(
            self.client.post(self.review_url(preview), self.new_people()).status_code,
            409,
        )
        self.assertEqual(Person.objects.count(), before)
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    def test_retained_document_intake_timezone_confirmation_and_safe_context(self):
        preview = self.prepare(
            message_csv({"Reactions": '<script>alert("x")</script>'})
        )
        document = DocumentEvidence.objects.get()
        page = self.client.get(self.upload_url() + f"?document={document.pk}")
        self.assertEqual(page.context["form"]["document"].value(), document.pk)
        invalid = self.client.post(
            self.upload_url(),
            {
                "submission_id": str(uuid4()),
                "document": str(document.pk),
                "timezone": "Unknown/zone",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("confirm_timezone", invalid.context["form"].errors)
        self.assertIn("timezone", invalid.context["form"].errors)
        both = self.client.post(
            self.upload_url(),
            {
                "submission_id": str(uuid4()),
                "document": str(document.pk),
                "file": SimpleUploadedFile("another.csv", message_csv()),
                "timezone": "UTC",
                "confirm_timezone": "on",
            },
        )
        self.assertContains(
            both,
            "Upload one CSV or choose one retained document.",
            status_code=400,
        )
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        response = self.client.get(self.review_url(preview) + "?page=1")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, '<script>alert("x")</script>')
        self.assertEqual(
            self.client.get(self.upload_url() + "?document=invalid").status_code, 404
        )
        foreign = DocumentEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Foreign CSV",
            file=SimpleUploadedFile("foreign.csv", message_csv()),
        )
        self.assertEqual(
            self.client.get(self.upload_url() + f"?document={foreign.pk}").status_code,
            404,
        )

    def test_invalid_confirmation_preserves_draft_and_source_removal_hides_rows(self):
        preview = self.prepare()
        invalid = self.client.post(
            self.review_url(preview),
            self.new_people()
            | {
                "account-first_name": "Preserved draft",
                "account-email": "unfinished draft",
                "confirm": "",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(
            invalid.context["account_form"]["first_name"].value(), "Preserved draft"
        )
        self.assertIn("email", invalid.context["account_form"].errors)
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )
        DocumentEvidence.objects.update(case=self.other_case)
        removed = self.client.get(self.review_url(preview))
        self.assertNotContains(removed, "Original café")
        self.assertNotContains(removed, 'name="account-person"')

    def test_changed_source_hash_hides_rows_and_retry_follows_original_preview(self):
        preview = self.prepare()
        self.client.post(self.review_url(preview), self.new_people())
        final = AnalysisRun.objects.get(parameters__phase="import")
        AnalysisRun.objects.filter(pk=final.pk).update(
            status="failed", error_code="source_unavailable"
        )
        failed = self.client.get(self.review_url(final))
        self.assertContains(failed, "?processing=1")
        retried = self.client.post(
            reverse("core:analysis:retry", args=[self.case.pk, final.pk]),
            {
                "submission_id": str(uuid4()),
            },
        )
        self.assertEqual(retried.status_code, 302)
        attempt = AnalysisRun.objects.filter(previous_run=final).get()
        page = self.client.get(self.review_url(attempt))
        self.assertEqual(page.context["preview"].pk, preview.pk)
        self.assertEqual(page.context["run"].pk, attempt.pk)
        DocumentEvidence.objects.update(sha256="0" * 64)
        changed = self.client.get(self.review_url(attempt))
        self.assertNotContains(changed, "Original café")
        self.assertNotContains(changed, 'name="account-person"')

    def test_large_compact_review_has_no_row_cap_and_paginates_source_rows(self):
        raw = message_csv(
            *({"Text": f"Large message {index} " + "." * 4200} for index in range(551))
        )
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        preview = self.prepare(raw)
        self.assertEqual(preview.status, "complete", preview.error_code)
        self.assertEqual(preview.result.data["items"], [])
        self.assertEqual(preview.result.data["metrics"]["source_rows"], 551)
        response = self.client.get(self.review_url(preview) + "?page=23")
        rows = response.context["rows"]
        self.assertEqual(rows.paginator.count, 551)
        self.assertEqual(len(rows.object_list), 1)
        self.assertEqual(rows.object_list[0]["values"]["source_row"], 552)
        self.assertEqual(len(response.context["samples"]), 3)
        self.assertContains(response, "Page 23 of 23")
        self.assertContains(response, "Large message 550")
        self.assertNotContains(response, "Large message 500")

    def test_new_message_list_is_paginated_with_publication_counts(self):
        preview = self.prepare(
            message_csv(*({"Text": f"Message {index}"} for index in range(31)))
        )
        self.client.post(self.review_url(preview), self.new_people())
        final = AnalysisRun.objects.get(parameters__phase="import")
        process_run(final.pk)
        final.refresh_from_db()
        self.assertEqual(final.status, "complete", final.error_code)
        first = self.client.get(self.review_url(final))
        self.assertEqual(first.context["imported"].paginator.count, 31)
        self.assertEqual(len(first.context["imported"].object_list), 25)
        self.assertContains(first, "31 new messages added")
        self.assertContains(first, "View conversation")
        self.assertContains(
            first, '<details class="disclosure" id="imported-messages">'
        )
        self.assertLess(
            first.content.index(b"View conversation"),
            first.content.index(b"Source and import details"),
        )
        self.assertContains(
            self.client.get(first.context["conversation_url"]), "31 messages"
        )
        second = self.client.get(self.review_url(final) + "?messages_page=2")
        self.assertEqual(len(second.context["imported"].object_list), 6)
        self.assertContains(second, "Messages page 2 of 2")
        self.assertContains(
            second, '<details class="disclosure" id="imported-messages" open>'
        )

    def test_legacy_preview_stays_readable_and_requires_a_new_review(self):
        preview = self.prepare()
        data = deepcopy(preview.result.data)
        data["items"] = list(
            iter_import_rows(resolve_inputs(preview, self.case)[0], preview.parameters)
        )
        AnalysisResult.objects.filter(run=preview).update(data=data)
        AnalysisRun.objects.filter(pk=preview.pk).update(method_version="1")
        page = self.client.get(self.review_url(preview))
        self.assertContains(page, "Original café")
        self.assertContains(page, "earlier CSV review is retained for inspection")
        self.assertNotContains(page, 'name="account-person"')
        self.assertEqual(
            self.client.post(self.review_url(preview), self.new_people()).status_code,
            409,
        )
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    def test_review_uses_retained_text_limit_and_withholds_changed_file_bytes(self):
        body = "Retained message " * 200
        with self.settings(PANDECT_MAX_TEXT_BYTES=6000):
            preview = self.prepare(message_csv({"Text": body}))
        with self.settings(PANDECT_MAX_TEXT_BYTES=1024):
            page = self.client.get(self.review_url(preview) + "?page=1")
        self.assertEqual(page.context["metrics"]["ready_rows"], 1)
        self.assertEqual(page.context["rows"].object_list[0]["text"], body)
        document = DocumentEvidence.objects.get()
        with document.file.storage.open(document.file.name, "wb") as stream:
            stream.write(message_csv({"Text": "Changed source bytes"}))
        changed = self.client.get(self.review_url(preview) + "?page=1")
        self.assertEqual(changed.context["samples"], [])
        self.assertNotContains(changed, "Retained message")
        self.assertNotContains(changed, "Changed source bytes")
        self.assertNotContains(changed, 'name="account-person"')

    def test_summary_and_progress_never_parse_the_csv(self):
        preview = self.prepare()
        with patch("phone.imessage_views.read_import_page") as read:
            summary = self.client.get(self.review_url(preview))
            self.assertContains(summary, "Retained excerpts")
            self.assertContains(summary, "Review all message rows")
            self.assertEqual(list(summary.context["rows"]), [])
            read.assert_not_called()
        self.client.post(self.review_url(preview), self.new_people())
        final = AnalysisRun.objects.get(parameters__phase="import")
        with (
            patch("phone.imessage_views.preview_source") as source,
            patch("phone.imessage_views.read_import_page") as read,
        ):
            for run in (preview, final):
                response = self.client.get(
                    self.review_url(run) + "?page=1", HTTP_X_PANDECT_LIVE="1"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["X-Pandect-Live"], "1")
                self.assertIn("no-store", response["Cache-Control"])
                self.assertNotContains(response, "Original café")
            source.assert_not_called()
            read.assert_not_called()
        AnalysisRun.objects.filter(pk=final.pk).update(status="complete")
        receipt = "?reviews=messages&history_page=2"
        ready = self.client.get(
            self.review_url(final) + receipt, HTTP_X_PANDECT_LIVE="1"
        )
        self.assertContains(ready, "Open message import")
        self.assertContains(ready, 'data-live-poll="false"')
        self.assertContains(
            ready, self.review_url(final) + "?reviews=messages&amp;history_page=2"
        )
        self.assertContains(ready, "?reviews=messages&amp;page=2#processing-history")

    def test_processing_return_survives_cancel_confirmation_and_recovery(self):
        preview = self.prepare()
        review_query = {"reviews": "messages", "history_page": "2"}
        origin = reverse(
            "core:analysis:processing",
            args=(self.case.pk,),
            query={"reviews": "messages", "page": "2"},
            fragment="processing-history",
        )
        review_url = reverse(
            "phone:imessage_review", args=(self.case.pk, preview.pk), query=review_query
        )
        review = self.client.get(review_url)
        self.assertContains(review, f'href="{origin.replace("&", "&amp;")}"', count=2)
        confirmed = self.client.post(review_url, self.new_people())
        final = AnalysisRun.objects.get(parameters__phase="import")
        self.assertRedirects(
            confirmed,
            reverse(
                "phone:imessage_review",
                args=(self.case.pk, final.pk),
                query=review_query,
            ),
        )
        AnalysisRun.objects.filter(pk=final.pk).update(status="failed")
        stopped = self.client.get(confirmed.url)
        recovery = self.client.get(stopped.context["processing_details_url"])
        self.assertContains(recovery, f'href="{origin.replace("&", "&amp;")}"')

    def test_publication_verifies_changed_bytes_and_interpretation_after_confirmation(
        self,
    ):
        for drift in ("bytes", "interpretation"):
            with self.subTest(drift=drift):
                preview = self.prepare()
                if drift == "bytes":
                    document = DocumentEvidence.objects.get(
                        pk=preview.inputs[0]["evidence_id"]
                    )
                    with document.file.storage.open(document.file.name, "wb") as stream:
                        stream.write(message_csv({"Text": "Changed source bytes"}))
                else:
                    data = deepcopy(preview.result.data)
                    data["imessage"]["interpretation_sha256"] = "0" * 64
                    AnalysisResult.objects.filter(run=preview).update(data=data)
                with patch("phone.imessage_views.read_import_page") as read:
                    accepted = self.client.post(
                        self.review_url(preview), self.new_people()
                    )
                    read.assert_not_called()
                self.assertEqual(accepted.status_code, 302)
                final = AnalysisRun.objects.get(
                    parameters__phase="import", previous_run=preview
                )
                process_run(final.pk)
                final.refresh_from_db()
                self.assertEqual(final.status, "failed")
                self.assertFalse(TextMessageEvidence.objects.exists())
                self.assertFalse(CaseAnalysisBatch.objects.exists())
                response = self.client.get(accepted.url)
                self.assertContains(response, "The original remains retained.")
                AnalysisRun.objects.all().delete()
                DocumentEvidence.objects.all().delete()

    def test_progress_rechecks_case_access(self):
        preview = self.prepare()
        for role, status in (("viewer", 200), ("outsider", 404)):
            self.login(role)
            response = self.client.get(
                self.review_url(preview), HTTP_X_PANDECT_LIVE="1"
            )
            self.assertEqual(response.status_code, status)
        self.login("editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(
            self.client.get(
                self.review_url(preview), HTTP_X_PANDECT_LIVE="1"
            ).status_code,
            404,
        )

    @override_settings(ACCOUNT_RATE_LIMITS={"imessage_row_review": "2/m/user"})
    def test_full_csv_reviews_and_confirmation_share_the_user_limit(self):
        preview = self.prepare()
        for _ in range(2):
            self.assertEqual(
                self.client.get(self.review_url(preview) + "?page=1").status_code,
                200,
            )
        with patch("phone.imessage_views.read_import_page") as read:
            self.assertEqual(
                self.client.get(self.review_url(preview) + "?page=1").status_code,
                429,
            )
            self.assertEqual(
                self.client.post(
                    self.review_url(preview), self.new_people()
                ).status_code,
                429,
            )
            # Routine navigation and automatic progress remain available.
            self.assertEqual(self.client.get(self.review_url(preview)).status_code, 200)
            self.assertEqual(
                self.client.get(
                    self.review_url(preview) + "?page=1", HTTP_X_PANDECT_LIVE="1"
                ).status_code,
                200,
            )
            read.assert_not_called()
        self.assertFalse(
            AnalysisRun.objects.filter(parameters__phase="import").exists()
        )

    @override_settings(ACCOUNT_RATE_LIMITS={"imessage_row_review": "2/m/key"})
    def test_full_csv_reviews_share_the_case_limit_across_members(self):
        preview = self.prepare()
        for role, status in (("owner", 200), ("editor", 200), ("viewer", 429)):
            self.login(role)
            response = self.client.get(self.review_url(preview) + "?page=1")
            self.assertEqual(response.status_code, status)
        self.login("outsider")
        self.assertEqual(
            self.client.get(self.review_url(preview) + "?page=1").status_code, 404
        )

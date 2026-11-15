import json
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import Http404
from django.test import TestCase
from django.urls import reverse

from cases.models import Case, CaseMembership
from core.analysis.bulk import prepare_case_analysis_batch, start_case_analysis
from core.analysis.errors import AnalysisError
from core.analysis.import_checks import imported_sources
from core.analysis.operations import get_operation
from core.analysis.services import process_run, resolve_inputs, start_run
from core.analysis.sources import Source
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.analysis.deterministic.imports import (
    PARSER_VERSION,
    apply_import,
    prepare_import,
)
from phone.models import CommunicationContext, TextMessageEvidence


class CommunicationImportTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.other_person = Person.objects.create(
            case=self.case, first_name="Second person"
        )

    def document(self, html):
        item = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic HTML export",
            file=ContentFile(html.encode(), name="messages.html"),
        )
        return Source(str(uuid4()), "document", item)

    def row(
        self,
        body="Hello &amp; goodbye",
        *,
        sender="Me",
        timestamp="Oct 27, 2026  12:30:00 PM",
        attrs="",
        extra="",
    ):
        return (
            f'<div class="message" {attrs}>'
            f'<span class="timestamp">{timestamp}</span>'
            f'<span class="sender">{sender}</span>'
            f'<span class="bubble">{body}</span>{extra}</div>'
        )

    def parameters(self):
        return {
            "timezone": "America/Toronto",
            "sender_map": {
                "Me": str(self.person.pk),
                "Other account": str(self.other_person.pk),
            },
            "recipient_map": {
                "Me": str(self.other_person.pk),
                "Other account": str(self.person.pk),
            },
        }

    def run_result(self, source, data, *, actor="owner", parameters=None):
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users[actor],
            operation="conversation_import",
            method_version="imessage-html-v1",
            inputs=[
                {
                    "id": source.id,
                    "kind": "document",
                    "evidence_id": str(source.item.pk),
                    "sha256": source.item.sha256,
                }
            ],
            parameters=parameters or self.parameters(),
        )
        result = AnalysisResult.objects.create(case=self.case, run=run, data=data)
        return run, result

    def test_supported_export_preserves_text_context_and_original(self):
        html = self.row(
            attrs=(
                'data-message-id="m1" data-thread-id="t1" '
                'data-reply-to="m0" data-participants=\'["Me","Other '
                'account","Third account"]\' data-edited="true"'
            ),
            extra=(
                '<a class="attachment" '
                'href="../../private/image.jpg">Image reference</a>'
            ),
        )
        source = self.document(html)
        before = source.item.sha256
        result = prepare_import([source], self.parameters())
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["items"][0]["text"], "Hello & goodbye")
        values = result["items"][0]["values"]
        self.assertEqual(values["native_message_id"], "m1")
        self.assertEqual(
            values["participants"], ["Me", "Other account", "Third account"]
        )
        self.assertEqual(
            values["import_message"]["occurred_at"], "2026-10-27T16:30:00+00:00"
        )
        run, retained = self.run_result(source, result)
        with transaction.atomic():
            self.assertEqual(apply_import(run, retained.data, [source]), 1)
        message = TextMessageEvidence.objects.get()
        self.assertFalse(
            AnalysisRun.objects.filter(
                case=self.case,
                inputs__contains=[
                    {"kind": "text-message", "evidence_id": str(message.pk)},
                ],
            ).exists()
        )
        self.assertEqual(message.body, "Hello & goodbye")
        context = message.communication_context
        self.assertEqual(context.source_document, source.item)
        self.assertEqual(context.source_start, 0)
        self.assertEqual(context.source_end, len(html))
        self.assertEqual(context.native_thread_id, "t1")
        self.assertEqual(context.reply_to_id, "m0")
        self.assertEqual(
            context.attachments[0]["availability"],
            "not ingested; source reference only",
        )
        self.assertEqual(context.source_context, {"data-edited": "true"})
        source.item.refresh_from_db()
        self.assertEqual(source.item.sha256, before)
        with source.item.file.open("rb") as original:
            self.assertEqual(original.read(), html.encode())

    def test_source_spans_follow_html_line_positions_and_exact_closing_markup(self):
        for separator in ("\n", "\r\n", "\r", "\u0085", "\u2028", "\v"):
            with self.subTest(separator=repr(separator)):
                row = self.row(body="First<br/>second").replace("</div>", "</DIV \n>")
                html = f"<html>{separator}<p>Prefix</p>\n{row}</html>"
                source = self.document(html)
                data = prepare_import([source], self.parameters())
                values = data["items"][0]["values"]
                self.assertEqual(data["items"][0]["text"], "First\nsecond")
                self.assertEqual(values["parser_version"], PARSER_VERSION)
                self.assertEqual(
                    html[values["source_start"] : values["source_end"]], row
                )
                run, result = self.run_result(source, data)
                with transaction.atomic():
                    apply_import(run, result.data, [source])
                context = CommunicationContext.objects.get(source_document=source.item)
                self.assertEqual(html[context.source_start : context.source_end], row)

    def test_import_result_shows_mapping_time_and_unresolved_rows(self):
        source = self.document(self.row() + self.row(sender="Unmapped source"))
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "conversation_import",
            [{"kind": "document", "id": str(source.item.pk)}],
            parameters=self.parameters(),
        )
        process_run(run.pk)
        self.login("viewer")
        response = self.client.get(
            reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        )
        self.assertContains(response, "Reported sender")
        self.assertContains(response, "2026-10-27T16:30:00+00:00")
        self.assertContains(response, "Unresolved message row 2")
        self.assertContains(response, "Unresolved source-label mapping")
        self.assertContains(response, "Source row 2")
        self.assertNotContains(response, str(self.person.pk))

    def test_old_queued_import_version_requires_a_new_run_without_importing(self):
        source = self.document(self.row())
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "conversation_import",
            [{"kind": "document", "id": str(source.item.pk)}],
            parameters=self.parameters(),
        )
        self.assertEqual(run.method_version, "3")
        AnalysisRun.objects.filter(pk=run.pk).update(method_version="2")
        process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "processing_version_unavailable")
        self.assertFalse(TextMessageEvidence.objects.exists())
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_local_checks_scope_only_new_rows_from_exact_import_publication(self):
        source = self.document(self.row() + self.row(sender="Later mapped"))

        def import_rows(parameters):
            run, _ = start_run(
                self.users["owner"],
                self.case.pk,
                "conversation_import",
                [{"kind": "document", "id": str(source.item.pk)}],
                parameters=parameters,
            )
            process_run(run.pk)
            run.refresh_from_db()
            self.assertIn(run.status, {"complete", "partial"}, run.error_code)
            return run, {"type": "messages", "id": str(run.pk)}

        first, first_scope = import_rows(self.parameters())
        later_parameters = self.parameters()
        later_parameters["sender_map"]["Later mapped"] = str(self.person.pk)
        later_parameters["recipient_map"]["Later mapped"] = str(self.other_person.pk)
        second, second_scope = import_rows(later_parameters)
        _, duplicate_scope = import_rows(later_parameters)
        first_rows = imported_sources(self.case, first_scope)[1]
        second_rows = imported_sources(self.case, second_scope)[1]
        self.assertEqual(first_rows.count(), 1)
        self.assertEqual(second_rows.count(), 1)
        self.assertNotEqual(first_rows.get().pk, second_rows.get().pk)
        self.assertFalse(imported_sources(self.case, duplicate_scope)[1].exists())
        with self.assertRaisesMessage(ValidationError, "no newly added evidence"):
            start_case_analysis(
                self.users["owner"],
                self.case.pk,
                submission_id=uuid4(),
                scope=duplicate_scope,
            )
        batch, _ = start_case_analysis(
            self.users["owner"], self.case.pk, submission_id=uuid4(), scope=first_scope
        )
        prepare_case_analysis_batch(batch.pk)
        self.assertEqual(batch.runs.count(), 2)
        for check in batch.runs.all():
            self.assertEqual(
                [item["evidence_id"] for item in check.inputs],
                [str(first_rows.get().pk)],
            )
            self.assertFalse(check.external_processing)
        self.login("owner")
        detail = self.client.get(
            reverse("core:analysis:detail", args=[self.case.pk, first.pk]),
            {"finding": 1},
        )
        self.assertContains(detail, "1 imported record")
        self.assertContains(detail, "Run local checks on imported evidence")
        historical = dict(second.result.data)
        historical.pop("import_publication_version")
        AnalysisResult.objects.filter(run=second).update(data=historical)
        with self.assertRaises(AnalysisError) as error:
            imported_sources(self.case, second_scope)
        self.assertEqual(error.exception.code, "import_membership_unknown")
        detail = self.client.get(
            reverse("core:analysis:detail", args=[self.case.pk, second.pk])
        )
        self.assertContains(detail, "historical import does not record")
        self.assertNotContains(detail, "Run local checks on imported evidence")

    def test_added_publication_metadata_obeys_result_bound_and_rolls_back(self):
        source = self.document(self.row())
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "conversation_import",
            [{"kind": "document", "id": str(source.item.pk)}],
            parameters=self.parameters(),
        )
        data = prepare_import(resolve_inputs(run, self.case), self.parameters())
        data["padding"] = ""
        remaining = 4 * 1024 * 1024 - len(json.dumps(data).encode()) - 1
        data["padding"] = "x" * remaining
        operation = replace(
            get_operation("conversation_import"), function=lambda *_: data
        )
        with patch("core.analysis.services.get_operation", return_value=operation):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_code, "result_limit")
        self.assertFalse(TextMessageEvidence.objects.filter(case=self.case).exists())
        self.assertFalse(CommunicationContext.objects.exists())
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_new_parser_runs_preserve_previously_imported_context(self):
        source = self.document(self.row())
        data = prepare_import([source], self.parameters())
        data["items"][0]["values"]["parser_version"] = "imessage-html-v1"
        run, result = self.run_result(source, data)
        with transaction.atomic():
            self.assertEqual(apply_import(run, result.data, [source]), 1)
        revised = prepare_import([source], self.parameters())
        next_run, next_result = self.run_result(source, revised)
        with transaction.atomic():
            self.assertEqual(apply_import(next_run, next_result.data, [source]), 0)
        context = CommunicationContext.objects.get(source_document=source.item)
        self.assertEqual(context.parser_version, "imessage-html-v1")
        self.assertEqual(TextMessageEvidence.objects.count(), 1)

    def test_retries_do_not_duplicate_and_conflicting_mapping_is_rejected(self):
        source = self.document(self.row())
        data = prepare_import([source], self.parameters())
        run, result = self.run_result(source, data)
        with transaction.atomic():
            self.assertEqual(apply_import(run, result.data, [source]), 1)
            self.assertEqual(apply_import(run, result.data, [source]), 0)
        self.assertEqual(TextMessageEvidence.objects.count(), 1)
        changed = self.parameters()
        changed["sender_map"]["Me"] = str(self.other_person.pk)
        modified = prepare_import([source], changed)
        second, second_result = self.run_result(source, modified, parameters=changed)
        with self.assertRaises(ValidationError), transaction.atomic():
            apply_import(second, second_result.data, [source])
        self.assertEqual(TextMessageEvidence.objects.count(), 1)
        self.assertEqual(CommunicationContext.objects.count(), 1)

    def test_retry_rejects_a_retained_message_link_outside_the_case(self):
        source = self.document(self.row())
        data = prepare_import([source], self.parameters())
        run, result = self.run_result(source, data)
        with transaction.atomic():
            self.assertEqual(apply_import(run, result.data, [source]), 1)
        other_case = Case.objects.create(owner=run.requested_by, name="Unrelated case")
        TextMessageEvidence.objects.filter(case=self.case).update(case=other_case)
        with (
            self.assertRaisesMessage(ValidationError, "inconsistent retained message"),
            transaction.atomic(),
        ):
            apply_import(run, result.data, [source])
        self.assertEqual(TextMessageEvidence.objects.count(), 1)

    def test_unresolved_labels_and_missing_native_fields_stay_unknown(self):
        source = self.document(self.row(sender="Third distinct account"))
        result = prepare_import([source], self.parameters())
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["unresolved_rows"], 1)
        values = result["items"][0]["values"]
        self.assertEqual(values["raw_sender"], "Third distinct account")
        self.assertEqual(values["native_message_id"], "")
        self.assertEqual(values["participants"], [])
        self.assertNotIn("import_message", values)

    def test_valueless_optional_attributes_do_not_prevent_parsing(self):
        source = self.document(
            self.row(
                attrs="data-edited",
                extra='<span class></span><a class="attachment" href>Reference</a>',
            )
        )
        result = prepare_import([source], self.parameters())
        self.assertEqual(result["outcome"], "complete")
        values = result["items"][0]["values"]
        self.assertEqual(values["source_context"], {"data-edited": ""})
        self.assertEqual(values["attachments"][0]["href"], "")

    def test_oversized_timestamp_does_not_block_valid_rows(self):
        source = self.document(
            self.row()
            + self.row(timestamp="Oct 27, 2026  12:30:00 PM (" + "x" * 256 + ")")
        )
        result = prepare_import([source], self.parameters())
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["metrics"]["ready_rows"], 1)
        run, retained = self.run_result(source, result)
        with transaction.atomic():
            self.assertEqual(apply_import(run, retained.data, [source]), 1)

    def test_uuid_spelling_does_not_create_a_conflicting_mapping(self):
        source = self.document(self.row())
        parameters = self.parameters()
        parameters["sender_map"]["Me"] = self.person.pk.hex.upper()
        parameters["recipient_map"]["Me"] = self.other_person.pk.hex
        result = prepare_import([source], parameters)
        run, retained = self.run_result(source, result, parameters=parameters)
        with transaction.atomic():
            self.assertEqual(apply_import(run, retained.data, [source]), 1)
            self.assertEqual(apply_import(run, retained.data, [source]), 0)
        self.assertEqual(TextMessageEvidence.objects.get().sender_id, self.person.pk)

    def test_large_supported_export_publishes_without_redundant_body_copies(self):
        body = "x" * (768 * 1024)
        source = self.document(self.row(body) + self.row(body, sender="Other account"))
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "conversation_import",
            [{"kind": "document", "id": str(source.item.pk)}],
            parameters=self.parameters(),
        )
        process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "complete")
        self.assertEqual(TextMessageEvidence.objects.filter(body=body).count(), 2)

    def test_ambiguous_and_nonexistent_time_need_review(self):
        for timestamp in (
            "Nov 01, 2026  01:30:00 AM",
            "Mar 08, 2026  02:30:00 AM",
            "not a time",
        ):
            with self.subTest(timestamp=timestamp):
                result = prepare_import(
                    [self.document(self.row(timestamp=timestamp))], self.parameters()
                )
                self.assertEqual(result["outcome"], "partial")
                self.assertNotIn("import_message", result["items"][0]["values"])
        explicit = prepare_import(
            [self.document(self.row(timestamp="2026-11-01T01:30:00-04:00"))],
            self.parameters(),
        )
        self.assertEqual(
            explicit["items"][0]["values"]["import_message"]["occurred_at"],
            "2026-11-01T05:30:00+00:00",
        )

    def test_crosscase_mappings_and_stale_source_rejected(self):
        foreign = Person.objects.create(case=self.other_case, first_name="Foreign")
        parameters = self.parameters()
        parameters["sender_map"]["Me"] = str(foreign.pk)
        source = self.document(self.row())
        with self.assertRaises(ValidationError):
            prepare_import([source], parameters)
        source.item.sha256 = "a" * 64
        with self.assertRaises(ValidationError):
            prepare_import([source], self.parameters())

    def test_role_revocation_and_atomic_rollback(self):
        source = self.document(self.row())
        run, result = self.run_result(
            source, prepare_import([source], self.parameters()), actor="editor"
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        with self.assertRaises(Http404), transaction.atomic():
            apply_import(run, result.data, [source])
        self.assertEqual(TextMessageEvidence.objects.count(), 0)
        owner_run, owner_result = self.run_result(source, result.data)
        with self.assertRaises(RuntimeError), transaction.atomic():
            apply_import(owner_run, owner_result.data, [source])
            raise RuntimeError("Synthetic publication interruption")
        self.assertEqual(TextMessageEvidence.objects.count(), 0)
        self.assertEqual(CommunicationContext.objects.count(), 0)

    def test_hostile_markup_is_text_and_references_are_never_loaded(self):
        source = self.document(
            self.row(
                body=(
                    "&lt;script&gt;alert(1)&lt;/script&gt;<script>ignored "
                    "code</script>\nKeep <b>visible</b> text.<br>Next line."
                ),
                extra=(
                    '<img class="attachment" '
                    'src="https://invalid.example.test/private">'
                ),
            )
        )
        result = prepare_import([source], self.parameters())
        text = result["items"][0]["text"]
        self.assertIn("<script>alert(1)</script>", text)
        self.assertNotIn("ignored code", text)
        self.assertIn("Keep visible text.\nNext line.", text)
        self.assertEqual(
            result["items"][0]["values"]["attachments"][0]["href"],
            "https://invalid.example.test/private",
        )

    def test_parser_limits_malformed_rows_and_immutable_context(self):
        with self.assertRaises(ValidationError):
            prepare_import(
                [self.document("<div>not the supported profile</div>")],
                self.parameters(),
            )
        with self.assertRaises(ValidationError):
            prepare_import(
                [self.document("<div class='message'><span>broken</div>")],
                self.parameters(),
            )
        partial = prepare_import(
            [
                self.document(
                    self.row()
                    + '<div class="message"><span class="sender">Me</span></div>'
                )
            ],
            self.parameters(),
        )
        self.assertEqual(partial["outcome"], "partial")
        self.assertEqual(partial["metrics"]["ready_rows"], 1)
        source = self.document(self.row())
        run, result = self.run_result(
            source, prepare_import([source], self.parameters())
        )
        with transaction.atomic():
            apply_import(run, result.data, [source])
        context = CommunicationContext.objects.get()
        context.native_thread_id = "invented"
        with self.assertRaises(ValidationError):
            context.save()

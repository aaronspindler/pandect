"""Confirmed imports automatically analyze exact published messages, recoverably."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.urls import reverse
from django_tasks_db.models import DBTaskResult

from cases.models import CaseMembership
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.errors import AnalysisError
from core.analysis.services import process_run, start_run
from core.models import CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.imazing_imports import confirm_import
from phone.models import PhoneCallEvidence, TextMessageEvidence
from phone.tests.test_imazing_views import message_csv


class ImportedMessageAnalysisTests(EvidenceViewTestMixin, TestCase):
    def publish(self, *rows):
        start = datetime(2026, 11, 1, 12, tzinfo=UTC)
        rows = rows or tuple(
            {
                "Text": f"Message {index}: meet at the harbour. person@example.test.",
                "Type": "Incoming" if index % 2 else "Outgoing",
                "Message Date": (start + timedelta(minutes=index)).isoformat(),
            }
            for index in range(6)
        )
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Synthetic message CSV",
            file=ContentFile(message_csv(*rows), name="synthetic-messages.csv"),
        )
        preview, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "imazing_import",
            [{"kind": "document", "id": str(document.pk)}],
            parameters={"phase": "preview", "timezone": "America/Toronto"},
        )
        process_run(preview.pk)
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        other = Person.objects.create(case=self.case, first_name="Correspondent")
        final, _ = confirm_import(
            self.users["editor"],
            self.case.pk,
            preview.pk,
            {
                "account": {"id": str(self.person.pk), "create": None},
                "correspondent": {"id": str(other.pk), "create": None},
            },
        )
        process_run(final.pk)
        final.refresh_from_db()
        return final

    def drain(self, batch):
        for _ in range(30):
            prepare_case_analysis_batch(batch.pk)
            batch.refresh_from_db()
            if batch.status != "queued":
                self.assertEqual(batch.status, "complete", batch.error_code)
                return
        self.fail("Import analysis preparation did not finish")

    def test_publication_queues_full_suite_once_and_reuses_no_rows(self):
        final = self.publish()
        self.assertEqual(final.status, "complete", final.error_code)
        batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(batch.scope["id"], str(final.pk))
        self.assertTrue(batch.scope["automatic"])
        self.assertFalse(batch.runs.exists())
        self.drain(batch)
        self.assertEqual(batch.processed_sources, 6)
        self.assertEqual(
            set(batch.runs.values_list("operation", flat=True)),
            {
                "entities",
                "text_topics",
                "sessions",
                "comm_patterns",
                "style_profiles",
                "style_change",
                "copied_text",
                "conversation_link",
                "content_events",
                "session_summaries",
            },
        )
        ids = set(TextMessageEvidence.objects.values_list("pk", flat=True))
        for run in batch.runs.all():
            self.assertEqual(run.requested_by_id, self.users["editor"].pk)
            self.assertFalse(run.external_processing)
            self.assertLessEqual(
                {entry["evidence_id"] for entry in run.inputs}, set(map(str, ids))
            )
            if "_execution" in run.parameters:
                self.assertEqual(run.parameters["_execution"]["backend"], "local")
            if run.operation in {"style_profiles", "style_change"}:
                self.assertEqual(
                    TextMessageEvidence.objects.filter(
                        pk__in=[entry["evidence_id"] for entry in run.inputs]
                    )
                    .values("sender_id")
                    .distinct()
                    .count(),
                    1,
                )
        tasks = DBTaskResult.objects.count()
        process_run(final.pk)
        self.assertEqual(DBTaskResult.objects.count(), tasks)
        repeated, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "imazing_import",
            [{"kind": "document", "id": final.inputs[0]["evidence_id"]}],
            parameters=final.parameters,
        )
        process_run(repeated.pk)
        self.assertEqual(CaseAnalysisBatch.objects.count(), 1)
        self.assertEqual(
            set(TextMessageEvidence.objects.values_list("pk", flat=True)), ids
        )

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=2)
    def test_small_windows_cover_every_message_including_unknown_times(self):
        final = self.publish(
            {"Text": "Third", "Message Date": "2026-11-01T12:03:00Z"},
            {"Text": "Unknown", "Message Date": ""},
            {"Text": "First", "Message Date": "2026-11-01T12:01:00Z"},
            {"Text": "Second", "Message Date": "2026-11-01T12:02:00Z"},
            {"Text": "Excluded", "Type": "Unsupported"},
        )
        self.assertEqual(final.status, "partial", final.error_code)
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        self.assertEqual(batch.processed_sources, 4)
        entity_runs = batch.runs.filter(operation="entities")
        self.assertEqual(entity_runs.count(), 2)
        ids = [entry["evidence_id"] for run in entity_runs for entry in run.inputs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            set(ids),
            set(map(str, TextMessageEvidence.objects.values_list("pk", flat=True))),
        )
        for run in entity_runs:
            process_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(
                run.result.data["metrics"]["import_selection"]["imported_messages"], 4
            )
            self.assertIn(
                "only its retained selection", " ".join(run.result.data["warnings"])
            )

    def test_writing_comparison_keeps_tied_times_together_at_a_valid_split(self):
        self.publish(
            *({"Message Date": "2026-11-01T12:00:00Z"} for _ in range(3)),
            {"Message Date": "2026-11-01T12:01:00Z"},
        )
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        run = batch.runs.get(operation="style_change")
        self.assertEqual(len(run.parameters["group_a"]), 3)
        self.assertEqual(len(run.parameters["group_b"]), 1)
        self.assertEqual(len(run.inputs), 4)

    @override_settings(PANDECT_LOCAL_AI={})
    def test_missing_local_runtime_does_not_block_rules_or_change_originals(self):
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        originals = list(
            TextMessageEvidence.objects.values_list("pk", "body", "sha256")
        )
        for run in batch.runs.all():
            if "_execution" in run.parameters:
                self.assertEqual(process_run(run.pk), "local_ai_not_configured")
            else:
                process_run(run.pk)
                run.refresh_from_db()
                self.assertIn(
                    run.status, {"complete", "partial", "empty", "insufficient"}
                )
        self.assertEqual(
            list(TextMessageEvidence.objects.values_list("pk", "body", "sha256")),
            originals,
        )

    def test_every_model_operation_publishes_validated_suggestions(self):
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)

        def infer(prompt, schema):
            evidence = json.loads(prompt.split("Evidence:\n", 1)[1])
            return {
                "items": [
                    {
                        "kind": "suggestion",
                        "text": "A reported meeting suggestion.",
                        "citations": [
                            {
                                "source_id": item["source_id"],
                                "quote": item["text"],
                                "start": 0,
                            }
                            for item in evidence[:2]
                        ],
                    }
                ]
            }

        with (
            patch("core.analysis.local.LocalClient._validate_configuration"),
            patch("core.analysis.local.LocalClient.infer", side_effect=infer),
        ):
            for run in batch.runs.filter(parameters__has_key="_execution"):
                process_run(run.pk)
                run.refresh_from_db()
                self.assertEqual(
                    run.status, "complete", (run.operation, run.error_code)
                )
                self.assertTrue(run.result.data["items"])
                self.assertEqual(run.result.provenance["execution"]["backend"], "local")

    def test_character_windows_preserve_every_oversized_original(self):
        self.publish(*({"Text": "x" * 130_000} for _ in range(9)))
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        self.assertEqual(batch.processed_sources, 9)
        self.assertEqual(batch.runs.filter(operation="entities").count(), 2)
        seen = []
        for run in batch.runs.filter(operation="entities"):
            messages = TextMessageEvidence.objects.filter(
                pk__in=[item["evidence_id"] for item in run.inputs]
            )
            self.assertLessEqual(sum(len(item.body) for item in messages), 1_000_000)
            seen.extend(messages.values_list("pk", flat=True))
        self.assertEqual(len(set(seen)), 9)
        self.assertTrue(
            all(
                len(body) == 130_000
                for body in TextMessageEvidence.objects.values_list("body", flat=True)
            )
        )

    def test_mocked_model_failure_and_retry_keep_independent_results(self):
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        run = batch.runs.get(operation="content_events")
        with (
            patch("core.analysis.local.LocalClient._validate_configuration"),
            patch(
                "core.analysis.local.LocalClient.infer",
                side_effect=AnalysisError("local_ai_unavailable"),
            ),
        ):
            self.assertEqual(process_run(run.pk), "local_ai_unavailable")
        self.assertEqual(TextMessageEvidence.objects.count(), 6)
        retry, _ = start_run(
            self.users["editor"],
            self.case.pk,
            run.operation,
            [
                {"kind": entry["kind"], "id": entry["evidence_id"]}
                for entry in run.inputs
            ],
            parameters=run.parameters,
            previous_run=run,
        )

        def infer(prompt, schema):
            evidence = json.loads(prompt.split("Evidence:\n", 1)[1])
            return {
                "items": [
                    {
                        "kind": "claim",
                        "text": "A reported meeting suggestion.",
                        "citations": [
                            {
                                "source_id": evidence[0]["source_id"],
                                "quote": evidence[0]["text"],
                                "start": 0,
                            }
                        ],
                    }
                ]
            }

        with (
            patch("core.analysis.local.LocalClient._validate_configuration"),
            patch("core.analysis.local.LocalClient.infer", side_effect=infer),
        ):
            process_run(retry.pk)
        retry.refresh_from_db()
        self.assertEqual(retry.status, "complete", retry.error_code)
        self.assertEqual(retry.batch_id, batch.pk)
        self.assertEqual(len(retry.result.data["items"]), 1)

    def test_revocation_stops_preparation_and_previously_queued_runs(self):
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        run = batch.runs.get(operation="entities")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(process_run(run.pk), "access_removed")
        batch.status = "queued"
        batch.save()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "access_removed")
        self.assertEqual(TextMessageEvidence.objects.count(), 6)

    def test_cross_channel_selection_excludes_other_cases_and_participants(self):
        self.publish()
        message = TextMessageEvidence.objects.filter(sender=self.person).first()
        values = {
            "case": self.case,
            "title": "Synthetic call",
            "uploaded_by": self.users["owner"],
            "sender": message.sender,
            "recipient": message.recipient,
            "occurred_at": message.occurred_at,
        }
        included = PhoneCallEvidence.objects.create(**values)
        PhoneCallEvidence.objects.create(**(values | {"recipient": self.person}))
        foreign = Person.objects.create(case=self.other_case, first_name="Other")
        PhoneCallEvidence.objects.create(
            **(
                values
                | {"case": self.other_case, "sender": foreign, "recipient": foreign}
            )
        )
        # The call must predate the automatic preparation snapshot.
        CaseAnalysisBatch.objects.update(
            created_at=included.created_at + timedelta(seconds=1)
        )
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        run = batch.runs.get(
            operation="conversation_link", parameters__time_window_minutes=10
        )
        self.assertEqual(
            [
                entry["evidence_id"]
                for entry in run.inputs
                if entry["kind"] == "phone-call"
            ],
            [str(included.pk)],
        )

    def test_preparation_enqueue_failure_rolls_back_publication(self):
        with patch(
            "core.analysis.tasks.prepare_case_analysis",
            **{"enqueue.side_effect": OperationalError},
        ):
            final = self.publish()
        self.assertEqual(final.error_code, "processing_failed")
        self.assertFalse(TextMessageEvidence.objects.exists())
        self.assertFalse(CaseAnalysisBatch.objects.exists())

    def test_review_links_automatic_batch_and_rerun_requires_edit_access(self):
        final = self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.login("viewer")
        review = reverse("phone:imazing_review", args=[self.case.pk, final.pk])
        response = self.client.get(review)
        self.assertContains(response, "Follow message analysis")
        self.assertNotContains(response, "Run message analysis again")
        endpoint = reverse(
            "core:analysis:import_checks", args=[self.case.pk, "messages", final.pk]
        )
        self.assertEqual(
            self.client.post(endpoint, {"submission_id": uuid4()}).status_code, 403
        )
        self.drain(batch)
        self.login("owner")
        response = self.client.post(endpoint, {"submission_id": uuid4()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CaseAnalysisBatch.objects.filter(scope=batch.scope).count(), 2)

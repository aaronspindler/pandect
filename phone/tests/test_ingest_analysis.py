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

from cases.models import Case, CaseMembership
from core.analysis.approval import revoke_case_execution
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.errors import AnalysisError
from core.analysis.services import process_run, start_run
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_execution import LOCAL
from core.analysis.tests.test_gateway import CONFIG
from core.models import AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.imessage_imports import confirm_import
from phone.models import PhoneCallEvidence, TextMessageEvidence
from phone.tests.test_imessage_views import message_csv, whatsapp_csv


@override_settings(PANDECT_LOCAL_AI={})
class ImportedMessageAnalysisTests(EvidenceViewTestMixin, TestCase):
    def select_external_ai(self):
        grant = external_grant_kwargs(self.case, "content_events")
        self.case.ai_backend = Case.AIBackend.GATEWAY
        self.case.save(update_fields=("ai_backend",))
        return grant

    def publish(self, *rows, csv_format=message_csv):
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
            file=ContentFile(csv_format(*rows), name="synthetic-messages.csv"),
        )
        preview, _ = start_run(
            self.users["editor"],
            self.case.pk,
            "imessage_import",
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

    @override_settings(PANDECT_AI_GATEWAY=CONFIG, PANDECT_LOCAL_AI=LOCAL)
    def test_import_runs_rules_and_one_ai_check_on_the_selected_external_backend(self):
        self.select_external_ai()
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        self.assertEqual(batch.runs.filter(operation="entities").count(), 1)
        self.assertEqual(batch.conversation_groupings.get().sessions.count(), 1)
        for operation in (
            "text_topics",
            "content_events",
            "session_summaries",
            "conversation_link",
        ):
            run = batch.runs.get(operation=operation, parameters__has_key="_execution")
            self.assertTrue(run.external_processing)
            self.assertEqual(
                run.parameters["_execution"],
                batch.scope["executions"][f"{operation}:gateway"],
            )
        self.assertEqual(batch.unavailable_sources, 0)

    @override_settings(PANDECT_AI_GATEWAY=CONFIG, PANDECT_LOCAL_AI=LOCAL)
    def test_private_ai_keeps_built_in_keyword_and_exchange_checks(self):
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        self.assertTrue(batch.runs.filter(operation="entities").exists())
        self.assertCountEqual(
            batch.runs.filter(parameters__has_key="_execution").values_list(
                "operation", "parameters__backend"
            ),
            [
                ("content_events", "local"),
                ("conversation_link", "local"),
                ("session_summaries", "local"),
                ("text_topics", "local"),
            ],
        )
        rules = batch.runs.exclude(parameters__has_key="_execution")
        self.assertEqual(
            rules.filter(
                operation="text_topics", parameters__include_summary=False
            ).count(),
            1,
        )
        self.assertEqual(rules.filter(operation="conversation_link").count(), 1)
        self.assertFalse(batch.runs.filter(external_processing=True).exists())
        self.assertEqual(batch.unavailable_sources, 0)

    def test_new_external_selection_does_not_expand_queued_import(self):
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        with self.settings(PANDECT_AI_GATEWAY=CONFIG):
            self.select_external_ai()
            self.drain(batch)
        self.assertTrue(batch.runs.filter(operation="entities").exists())
        self.assertFalse(batch.runs.filter(parameters__has_key="_execution").exists())

    @override_settings(PANDECT_AI_GATEWAY=CONFIG, PANDECT_LOCAL_AI=LOCAL)
    def test_destination_drift_does_not_switch_to_private_ai(self):
        self.select_external_ai()
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        with self.settings(PANDECT_AI_GATEWAY=CONFIG | {"models": {"text": "changed"}}):
            self.drain(batch)
        self.assertFalse(batch.runs.filter(parameters__has_key="_execution").exists())
        self.assertTrue(batch.runs.filter(operation="entities").exists())
        self.assertTrue(batch.preparation_errors)
        self.assertEqual(TextMessageEvidence.objects.count(), 6)

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_gateway_failure_preserves_import_and_allows_approved_retry(self):
        grant = self.select_external_ai()
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        run = batch.runs.get(operation="content_events")
        with patch(
            "core.analysis.gateway.GatewayClient.infer",
            side_effect=AnalysisError("gateway_unavailable"),
        ):
            self.assertEqual(process_run(run.pk), "gateway_unavailable")
        self.assertEqual(TextMessageEvidence.objects.count(), 6)
        self.assertTrue(batch.runs.filter(operation="entities").exists())
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
            **grant,
        )
        with patch(
            "core.analysis.gateway.GatewayClient.infer", return_value={"items": []}
        ):
            process_run(retry.pk)
        retry.refresh_from_db()
        self.assertEqual(retry.status, "empty")
        self.assertEqual(retry.batch_id, batch.pk)

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
                "comm_patterns",
                "style_profiles",
                "style_change",
                "copied_text",
                "conversation_link",
            },
        )
        ids = set(TextMessageEvidence.objects.values_list("pk", flat=True))
        for run in batch.runs.all():
            self.assertEqual(run.requested_by_id, self.users["editor"].pk)
            self.assertFalse(run.external_processing)
            self.assertLessEqual(
                {entry["evidence_id"] for entry in run.inputs}, set(map(str, ids))
            )
            self.assertNotIn("_execution", run.parameters)
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
            "imessage_import",
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

    def check_grouping_across_import_pages(self, csv_format):
        start = datetime(2026, 11, 1, 12, tzinfo=UTC)
        rows = []
        for index in range(28):
            elapsed = index + (90 if index >= 4 else 0) + (90 if index >= 25 else 0)
            if index == 27:
                elapsed += 250
            rows.append(
                {
                    "Message ID": f"id-{index}",
                    "Type": "Incoming" if index % 2 else "Outgoing",
                    "Text": {3: "Can you meet at six?", 4: "Yes, six works."}.get(
                        index, "Still organizing."
                    ),
                    "Replying to": "id-19" if index == 25 else "",
                    "Message Date": (start + timedelta(minutes=elapsed)).isoformat(),
                }
            )
        self.publish(*rows, csv_format=csv_format)
        batch = CaseAnalysisBatch.objects.get()
        # A delayed answer starts one worker page, an explicit reply another.
        with patch("phone.grouping_jobs.PAGE_SIZE", 5):
            self.drain(batch)
        grouping = batch.conversation_groupings.get()
        self.assertIsNotNone(grouping.published_at)
        first, later = grouping.sessions.order_by("start_ordinal")
        self.assertEqual((first.message_count, later.message_count), (27, 1))
        members = list(first.members.order_by("ordinal"))
        self.assertEqual(members[4].parent_message_id, members[3].message_id)
        self.assertEqual(members[25].parent_message_id, members[19].message_id)
        self.assertFalse(batch.runs.filter(operation="sessions").exists())

    def test_imessage_import_keeps_delayed_answers_and_replies_across_pages(self):
        self.check_grouping_across_import_pages(message_csv)

    def test_whatsapp_import_keeps_delayed_answers_and_replies_across_pages(self):
        self.check_grouping_across_import_pages(whatsapp_csv)

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

    @override_settings(PANDECT_LOCAL_AI=LOCAL)
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

    @override_settings(PANDECT_LOCAL_AI=LOCAL)
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

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_archiving_an_input_stops_queued_external_analysis_before_transfer(self):
        self.select_external_ai()
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        run = batch.runs.get(operation="content_events", external_processing=True)
        message = TextMessageEvidence.objects.get(pk=run.inputs[0]["evidence_id"])
        self.login("owner")
        response = self.client.post(self.url("archive", message, kind="text-message"))
        self.assertEqual(response.status_code, 302)
        with patch("core.analysis.gateway.GatewayClient.infer") as send:
            self.assertEqual(process_run(run.pk), "source_archived")
        send.assert_not_called()

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
        review = reverse("phone:imessage_review", args=[self.case.pk, final.pk])
        response = self.client.get(review)
        self.assertContains(response, "Follow message analysis")
        self.assertNotContains(response, "Run import analysis again")
        endpoint = reverse(
            "core:analysis:import_checks", args=[self.case.pk, "messages", final.pk]
        )
        self.assertEqual(
            self.client.post(endpoint, {"submission_id": uuid4()}).status_code, 403
        )
        self.drain(batch)
        self.login("owner")
        self.assertEqual(self.client.get(endpoint).status_code, 405)
        payload = {"submission_id": uuid4()}
        response = self.client.post(endpoint, payload)
        latest = CaseAnalysisBatch.objects.exclude(pk=batch.pk).get()
        status_url = reverse(
            "phone:imessage_analysis_status", args=[self.case.pk, latest.pk]
        )
        self.assertRedirects(response, status_url)
        self.assertRedirects(self.client.post(endpoint, payload), status_url)
        self.assertEqual(
            CaseAnalysisBatch.objects.filter(scope__id=str(final.pk)).count(), 2
        )

    def test_analysis_status_tracks_preparation_execution_and_failure(self):
        final = self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.login("viewer")
        url = reverse("phone:imessage_analysis_status", args=[self.case.pk, batch.pk])
        response = self.client.get(url)
        self.assertContains(response, "Preparing message analysis")
        self.assertContains(response, 'data-live-poll="true"')
        self.assertContains(
            response, reverse("phone:imessage_review", args=[self.case.pk, final.pk])
        )
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.drain(batch)
        response = self.client.get(url)
        self.assertContains(response, "Checks queued")
        self.assertContains(response, 'data-live-poll="true"')
        for run in batch.runs.all():
            self.assertContains(
                response, reverse("core:analysis:detail", args=[self.case.pk, run.pk])
            )
            process_run(run.pk)
        settled = self.client.get(url)
        insufficient = batch.runs.filter(status="insufficient").count()
        self.assertGreater(insufficient, 0)
        self.assertContains(settled, "Analysis finished with gaps")
        self.assertEqual(settled.context["insufficient_count"], insufficient)
        self.assertEqual(
            settled.context["completed_count"],
            batch.runs.filter(status__in=("complete", "empty")).count(),
        )
        self.assertContains(settled, "View conversation")
        self.assertContains(
            settled, '<details class="disclosure" id="message-analysis-details" open>'
        )
        conversation = self.client.get(settled.context["conversation_url"])
        self.assertEqual(conversation.status_code, 200)
        self.assertIsNone(conversation.context["selected_message"])
        batch.runs.update(status="failed", error_code="backend_unavailable")
        response = self.client.get(url, headers={"X-Pandect-Live": "1"})
        self.assertContains(response, 'data-live-poll="false"')
        self.assertContains(response, "Failed")
        self.assertContains(response, "Analysis finished with gaps")
        self.assertContains(response, f"{batch.runs.count()} checks stopped.")
        self.assertNotContains(response, "checks completed.")
        self.assertContains(
            response, '<details class="disclosure" id="message-analysis-details" open>'
        )
        self.assertNotContains(response, "<h1>")
        self.assertEqual(response["X-Pandect-Live"], "1")
        self.assertIn("no-store", response["Cache-Control"])
        batch.status = "failed"
        batch.error_code = "access_removed"
        batch.save()
        self.assertContains(self.client.get(url), "Preparation stopped")

    def test_analysis_status_is_case_scoped_read_only_and_paginated(self):
        self.publish()
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        selected = batch.runs.first()
        AnalysisRun.objects.bulk_create(
            AnalysisRun(
                case=self.case,
                requested_by=self.users["editor"],
                batch=batch,
                operation=selected.operation,
                method_version=selected.method_version,
                inputs=selected.inputs,
            )
            for _ in range(26)
        )
        unrelated = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="native_text",
            method_version="1",
        )
        foreign = CaseAnalysisBatch.objects.create(
            case=self.other_case, requested_by=self.users["outsider"], scope=batch.scope
        )
        url = reverse("phone:imessage_analysis_status", args=[self.case.pk, batch.pk])
        for role, expected in (
            ("owner", 200),
            ("editor", 200),
            ("viewer", 200),
            ("outsider", 404),
        ):
            self.login(role)
            for headers in ({}, {"X-Pandect-Live": "1"}):
                with self.subTest(role=role, headers=headers):
                    response = self.client.get(url, {"page": 2}, headers=headers)
                    self.assertEqual(response.status_code, expected)
                    if expected == 200:
                        self.assertEqual(response.context["page_obj"].number, 2)
                        self.assertEqual(
                            response.context["page_obj"].paginator.count,
                            batch.runs.count(),
                        )
                        self.assertNotContains(
                            response,
                            reverse(
                                "core:analysis:detail",
                                args=[self.case.pk, unrelated.pk],
                            ),
                        )
                        self.assertIn("private", response["Cache-Control"])
                        self.assertIn("no-store", response["Cache-Control"])
        self.login("editor")
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertEqual(
            self.client.get(
                reverse(
                    "phone:imessage_analysis_status", args=[self.case.pk, foreign.pk]
                )
            ).status_code,
            404,
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.get(url, headers={"X-Pandect-Live": "1"}).status_code, 404
        )

    @override_settings(PANDECT_AI_GATEWAY=CONFIG, PANDECT_LOCAL_AI=LOCAL)
    def test_approval_revocation_stops_import_ai_without_fallback(self):
        self.select_external_ai()
        final = self.publish()
        initial_batch = CaseAnalysisBatch.objects.get()
        self.drain(initial_batch)
        run = initial_batch.runs.get(operation="content_events")
        self.assertTrue(run.external_processing)
        revoke_case_execution(self.case)
        with patch("core.analysis.gateway.GatewayClient.infer") as send:
            self.assertEqual(process_run(run.pk), "external_processing_not_approved")
        send.assert_not_called()
        self.login("owner")
        endpoint = reverse(
            "core:analysis:import_checks", args=[self.case.pk, "messages", final.pk]
        )
        response = self.client.post(endpoint, {"submission_id": uuid4()})
        latest = CaseAnalysisBatch.objects.exclude(pk=initial_batch.pk).get()
        status_url = reverse(
            "phone:imessage_analysis_status", args=[self.case.pk, latest.pk]
        )
        self.assertRedirects(response, status_url)
        self.drain(latest)
        self.assertFalse(latest.runs.filter(parameters__has_key="_execution").exists())
        self.assertTrue(latest.runs.filter(operation="entities").exists())
        review = reverse("phone:imessage_review", args=[self.case.pk, final.pk])
        self.assertContains(self.client.get(review), status_url)

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_publication_retry_by_current_editor_uses_case_approval(self):
        self.select_external_ai()
        with patch(
            "core.analysis.tasks.prepare_case_analysis",
            **{"enqueue.side_effect": OperationalError},
        ):
            failed = self.publish()
        self.assertEqual(failed.status, "failed")
        self.assertFalse(TextMessageEvidence.objects.exists())
        retry, _ = start_run(
            self.users["owner"],
            self.case.pk,
            failed.operation,
            [{"kind": "document", "id": failed.inputs[0]["evidence_id"]}],
            parameters=failed.parameters,
            previous_run=failed,
        )
        process_run(retry.pk)
        retry.refresh_from_db()
        self.assertEqual(retry.status, "complete", retry.error_code)
        self.assertEqual(TextMessageEvidence.objects.count(), 6)
        batch = CaseAnalysisBatch.objects.get()
        self.drain(batch)
        self.assertTrue(batch.runs.filter(external_processing=True).exists())
        self.assertTrue(batch.runs.filter(operation="entities").exists())

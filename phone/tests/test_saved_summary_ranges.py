"""Exact coverage for long conversations across multiple bounded model runs."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.urls import reverse

from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.errors import AnalysisError
from core.analysis.services import process_run, resolve_inputs
from core.analysis.tests.test_execution import LOCAL
from core.models import CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from phone.grouping_jobs import grouping_scope, start_grouping
from phone.models import ConversationSession, TextMessageEvidence
from phone.saved_summary_ranges import coverage_url


@override_settings(PANDECT_LOCAL_AI=LOCAL, PANDECT_ANALYSIS_MAX_SOURCES=2)
class SavedSummaryRangeTests(EvidenceViewTestMixin, TestCase):
    def make_session(self, bodies):
        for index, body in enumerate(bodies):
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                sender=self.person,
                recipient=self.person,
                title=f"Synthetic message {index + 1}",
                body=body,
                occurred_at=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=index),
            )
        _, digest = grouping_scope(self.case, self.person, self.person)
        group, _ = start_grouping(
            self.users["owner"],
            self.case,
            self.person,
            self.person,
            gap_minutes=60,
            reviewed_scope=digest,
            submission_id=uuid4(),
        )
        self.assertEqual(prepare_case_analysis_batch(group.pk), "complete")
        return ConversationSession.objects.get(grouping__batch=group)

    def request_summary(self, session):
        url = reverse(
            "phone:instance_summary",
            args=(self.case.pk, self.person.pk, self.person.pk, session.pk),
        )
        review = self.client.get(url)
        payload = {
            "submission_id": uuid4(),
            "reviewed_scope": review.context["reviewed_scope"],
        }
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        batch = CaseAnalysisBatch.objects.get(pk=payload["submission_id"])
        for _ in range(10):
            status = prepare_case_analysis_batch(batch.pk)
            if status != "queued":
                break
        self.assertEqual(status, "complete")
        return batch

    def complete(self, run):
        source = resolve_inputs(run, self.case)[0]
        with (
            patch("core.analysis.local.LocalClient._validate_configuration"),
            patch(
                "core.analysis.local.LocalClient.infer",
                return_value={
                    "items": [
                        {
                            "text": "The sender provided a response.",
                            "citations": [
                                {"source_id": source.id, "quote": source.text}
                            ],
                        }
                    ]
                },
            ),
        ):
            process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete")
        self.assertTrue(run.result.data["metrics"]["range_covered"])

    def test_request_pins_ranges_and_reports_full_coverage_only_after_both_finish(self):
        session = self.make_session(["One", "Two", "Three"])
        self.login("editor")
        batch = self.request_summary(session)
        first, second = batch.runs.order_by("created_at")
        self.assertEqual(
            first.parameters["_saved_session_range"],
            {"session": str(session.pk), "first": 1, "last": 2},
        )
        self.assertEqual(len(second.inputs), 1)
        self.complete(first)
        url = coverage_url(self.case, session)
        self.assertContains(self.client.get(url), "Messages 1–2")
        self.assertContains(self.client.get(url), "Queued")
        reader = (
            reverse(
                "phone:conversation_detail",
                args=(self.case.pk, self.person.pk, self.person.pk),
            )
            + "?tab=instances"
        )
        self.assertContains(self.client.get(reader), "2 of 3 messages covered")
        self.complete(second)
        self.assertContains(self.client.get(reader), "3 of 3 messages covered")
        self.assertNotContains(self.client.get(reader), "Partial report")
        self.login("outsider")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_oversized_middle_original_leaves_visible_gap_and_doesnt_block_later_range(
        self,
    ):
        session = self.make_session(["Start", "a" * 16_001, "End"])
        self.login("owner")
        batch = self.request_summary(session)
        self.assertEqual(batch.runs.count(), 2)
        for run in batch.runs.all():
            self.complete(run)
        coverage = self.client.get(coverage_url(self.case, session))
        self.assertContains(coverage, "Message exceeds the summary limit")
        first = batch.runs.order_by("created_at").first()
        first.parameters["_saved_session_range"]["first"] = 3
        first.parameters["_saved_session_range"]["last"] = 3
        with self.assertRaises(AnalysisError):
            resolve_inputs(first, self.case)

    def test_request_retries_only_uncovered_ranges(self):
        session = self.make_session(["Start", "Continue", "Finish"])
        self.login("editor")
        batch = self.request_summary(session)
        first, last = batch.runs.order_by("created_at")
        self.complete(first)
        with (
            patch("core.analysis.local.LocalClient._validate_configuration"),
            patch(
                "core.analysis.local.LocalClient.infer",
                side_effect=AnalysisError("local_ai_unavailable"),
            ),
        ):
            self.assertEqual(process_run(last.pk), "local_ai_unavailable")
        retry = self.request_summary(session).runs.get()
        self.assertEqual(
            retry.parameters["_saved_session_range"],
            last.parameters["_saved_session_range"],
        )
        self.complete(retry)

    def test_blank_original_leaves_a_gap_without_resending_its_neighbours(self):
        session = self.make_session(["Start", "\t\u2007\n", "Finish"])
        self.login("owner")
        batch = self.request_summary(session)
        ranges = []
        for run in batch.runs.order_by("created_at"):
            scope = run.parameters["_saved_session_range"]
            ranges.append((scope["first"], scope["last"]))
            self.complete(run)
        self.assertEqual(ranges, [(1, 1), (3, 3)])
        coverage = self.client.get(coverage_url(self.case, session))
        self.assertContains(coverage, "Message 2")
        self.assertContains(coverage, "No text to summarize")
        self.assertFalse(self.request_summary(session).runs.exists())

    def test_archive_after_request_stops_transfer_for_that_range(self):
        session = self.make_session(["Start", "Finish"])
        self.login("editor")
        run = self.request_summary(session).runs.get()
        archive = reverse(
            "core:evidence:archive",
            args=(self.case.pk, "text-message", run.inputs[0]["evidence_id"]),
        )
        self.assertEqual(self.client.post(archive).status_code, 302)
        with patch("core.analysis.local.LocalClient.infer") as infer:
            self.assertEqual(process_run(run.pk), "source_archived")
        infer.assert_not_called()

    def test_new_range_size_and_archived_original_do_not_resend_covered_messages(self):
        session = self.make_session(["One", "Two", "Three", "Four", "Five"])
        self.login("owner")
        first, middle, last = self.request_summary(session).runs.order_by("created_at")
        self.complete(first)
        self.complete(last)
        with (
            patch("core.analysis.local.LocalClient._validate_configuration"),
            patch(
                "core.analysis.local.LocalClient.infer",
                side_effect=AnalysisError("local_ai_unavailable"),
            ),
        ):
            self.assertEqual(process_run(middle.pk), "local_ai_unavailable")
        archive = reverse(
            "core:evidence:archive",
            args=(self.case.pk, "text-message", first.inputs[0]["evidence_id"]),
        )
        self.assertEqual(self.client.post(archive).status_code, 302)
        with self.settings(PANDECT_ANALYSIS_MAX_SOURCES=5):
            retry = self.request_summary(session).runs.get()
        self.assertEqual(retry.inputs, middle.inputs)

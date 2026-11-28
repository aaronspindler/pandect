"""Case-wide retry preparation keeps retained selections and current access."""

from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.approval import revoke_case_execution
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.retries import (
    RETRIES_PER_STEP,
    latest_failed_retry,
    start_failed_retries,
)
from core.analysis.services import start_run
from core.analysis.tests.test_gateway import CONFIG
from core.analysis.tests.testing import external_grant_kwargs
from core.models import AnalysisRun, CaseAnalysisBatch
from core.tests.testing import EvidenceViewTestMixin
from text.models import TextEvidence


class FailedRetryTests(EvidenceViewTestMixin, TestCase):
    def run_for(self, source, operation="entities", status="failed", **kwargs):
        run, _ = start_run(
            source.uploaded_by,
            source.case_id,
            operation,
            [{"kind": "text", "id": str(source.pk)}],
            **kwargs,
        )
        if status != "queued":
            AnalysisRun.objects.filter(pk=run.pk).update(
                status=status, finished_at=timezone.now()
            )
        return run

    def request_retries(self, **kwargs):
        latest = latest_failed_retry(self.case)
        return start_failed_retries(
            self.users["editor"],
            self.case.pk,
            **(
                {
                    "submission_id": uuid4(),
                    "after_batch_id": latest.pk if latest else None,
                }
                | kwargs
            ),
        )

    def test_acknowledgment_preserves_failure_and_requires_current_editor_access(self):
        failed = self.run_for(self.text())
        url = reverse("core:analysis:acknowledge", args=[self.case.pk, failed.pk])
        for role, status in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(self.client.post(url).status_code, status)
        self.login("editor")
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 302)
        failed.refresh_from_db()
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.failure_acknowledged_by, self.users["editor"])
        first = failed.failure_acknowledged_at
        self.login("owner")
        self.assertEqual(self.client.post(url).status_code, 302)
        failed.refresh_from_db()
        self.assertEqual(failed.failure_acknowledged_at, first)
        self.assertEqual(self.request_retries(), (None, False))
        detail = self.client.get(
            reverse("core:analysis:detail", args=[self.case.pk, failed.pk])
        )
        self.assertContains(detail, "Missing coverage acknowledged")
        self.assertContains(detail, "Run again")
        completed = self.run_for(self.text(), status="complete")
        self.assertEqual(
            self.client.post(
                reverse("core:analysis:acknowledge", args=[self.case.pk, completed.pk])
            ).status_code,
            404,
        )

    def test_replay_of_a_coalesced_request_does_not_retry_new_failures(self):
        original = self.run_for(self.text())
        batch, _ = self.request_retries()
        stale = {"submission_id": uuid4(), "after_batch_id": None}
        self.assertEqual(self.request_retries(**stale), (batch, False))
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        AnalysisRun.objects.filter(previous_run=original).update(
            status="failed", finished_at=timezone.now()
        )
        self.assertEqual(self.request_retries(**stale), (batch, False))
        self.assertEqual(CaseAnalysisBatch.objects.filter(case=self.case).count(), 1)
        _, created = self.request_retries()
        self.assertTrue(created)

    def test_bounded_steps_reach_all_original_failures_once(self):
        note = self.text()
        original_batch = CaseAnalysisBatch.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            status=CaseAnalysisBatch.Status.COMPLETE,
        )
        failed = [
            self.run_for(note, batch=original_batch)
            for _ in range(3 * RETRIES_PER_STEP + 2)
        ]
        superseded = self.run_for(note)
        self.run_for(note, status="complete", previous_run=superseded)
        finishes_later = self.run_for(note, status="queued")
        foreign_note = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Other case original",
            body="Unrelated retained text.",
        )
        self.run_for(foreign_note)

        batch, created = self.request_retries()
        self.assertTrue(created)
        self.assertEqual(
            self.request_retries(submission_id=batch.submission_id), (batch, False)
        )
        self.assertEqual(self.request_retries(), (batch, False))
        AnalysisRun.objects.filter(pk=finishes_later.pk).update(
            status="failed", finished_at=timezone.now()
        )
        for step in range(1, 4):
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
            batch.refresh_from_db()
            self.assertEqual(batch.processed_sources, step * RETRIES_PER_STEP)
            self.assertEqual(batch.queued_runs, step * RETRIES_PER_STEP)
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        batch.refresh_from_db()
        self.assertEqual(batch.queued_runs, len(failed))
        self.assertEqual(batch.unavailable_sources, 0)
        successors = list(AnalysisRun.objects.filter(status="queued"))
        self.assertCountEqual(
            [run.previous_run_id for run in successors], [run.pk for run in failed]
        )
        for run in successors:
            self.assertEqual(run.inputs, run.previous_run.inputs)
            self.assertEqual(run.batch_id, original_batch.pk)
            self.assertEqual(run.requested_by, self.users["editor"])
        self.assertEqual(
            self.request_retries(submission_id=batch.submission_id), (batch, False)
        )

    def test_archived_and_changed_inputs_are_skipped_individually(self):
        active, archived, changed = self.text(), self.text(), self.text()
        eligible = self.run_for(active)
        old_archive = self.run_for(archived)
        old_version = self.run_for(changed)
        TextEvidence.objects.filter(pk=archived.pk).update(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        TextEvidence.objects.filter(pk=changed.pk).update(sha256="a" * 64)

        batch, _ = self.request_retries()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        batch.refresh_from_db()
        self.assertEqual((batch.processed_sources, batch.queued_runs), (3, 1))
        self.assertEqual(batch.unavailable_sources, 2)
        self.assertEqual(
            {row["run_id"] for row in batch.preparation_errors},
            {str(old_archive.pk), str(old_version.pk)},
        )
        self.assertEqual(
            AnalysisRun.objects.get(status="queued").previous_run, eligible
        )

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_external_retry_respects_the_approval_at_submission_and_preparation(self):
        note = self.text()
        original = self.run_for(
            note,
            "content_events",
            parameters={"backend": "gateway"},
            **external_grant_kwargs(self.case, "content_events"),
        )
        self.run_for(note)
        batch, _ = self.request_retries()
        revoke_case_execution(self.case)
        grant = external_grant_kwargs(self.case, "content_events")
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        batch.refresh_from_db()
        self.assertEqual((batch.queued_runs, batch.unavailable_sources), (1, 1))
        self.assertIn("case owner", batch.preparation_errors[0]["message"])
        self.assertFalse(AnalysisRun.objects.filter(previous_run=original).exists())

        batch, _ = self.request_retries()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        retry = AnalysisRun.objects.get(previous_run=original)
        self.assertTrue(retry.external_processing)
        self.assertEqual(retry.parameters["_execution"], grant["approved_execution"])
        self.assertEqual(
            retry.parameters["_external_approval"]["revision"],
            grant["external_grant_revision"],
        )
        self.assertEqual(retry.inputs, original.inputs)

    def test_revoked_case_access_stops_preparation_before_queueing(self):
        original = self.run_for(self.text())
        batch, _ = self.request_retries()
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "access_removed")
        batch.refresh_from_db()
        self.assertEqual(batch.status, "failed")
        self.assertFalse(AnalysisRun.objects.filter(previous_run=original).exists())

    def test_queue_error_rolls_back_the_page_and_another_request_can_retry(self):
        original = self.run_for(self.text())
        batch, _ = self.request_retries()
        with patch("core.analysis.tasks.process_analysis") as task:
            task.enqueue.side_effect = OSError
            self.assertEqual(
                prepare_case_analysis_batch(batch.pk), "preparation_failed"
            )
        batch.refresh_from_db()
        self.assertEqual(batch.status, "failed")
        self.assertEqual(batch.processed_sources, 0)
        self.assertFalse(AnalysisRun.objects.filter(previous_run=original).exists())

        batch, created = self.request_retries()
        self.assertTrue(created)
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(
            AnalysisRun.objects.get(status="queued").previous_run, original
        )

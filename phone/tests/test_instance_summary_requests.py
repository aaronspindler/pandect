"""Exact saved-session summary requests through the investigator workflow."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.approval import approve_scopes
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.catalogue import project_result
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    case_grant_revision,
    snapshot_execution,
    transfer_scope,
)
from core.analysis.operations import get_operation
from core.analysis.services import start_run
from core.analysis.source_resolution import resolve_inputs
from core.analysis.source_selection import snapshot_sources
from core.analysis.sources import source_id
from core.analysis.tests.test_gateway import TRANSCRIPTION_CONFIG
from core.analysis.tests.testing import external_grant_kwargs
from core.analysis.worker import process_run
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch
from core.tests.testing import EvidenceViewTestMixin
from phone.models import TextMessageEvidence


@override_settings(PANDECT_AI_GATEWAY=TRANSCRIPTION_CONFIG)
class InstanceSummaryRequestTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        external_grant_kwargs(self.case, "session_summaries")

    def message(self, minute, body="A synthetic message."):
        return TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            sender=self.person,
            recipient=self.person,
            title=f"Original {minute}",
            body=body,
            occurred_at=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=minute),
        )

    def save_sessions(self, originals, parameters=None):
        op = get_operation("sessions")
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": m.pk} for m in originals]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.case.owner,
            operation="sessions",
            method_version=op.version,
            status="complete",
            parameters=parameters or {},
            inputs=inputs,
        )
        result = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data=op.function(resolve_inputs(run, self.case), run.parameters),
        )
        project_result(result)
        return result

    def session(self, *originals, parameters=None):
        finding = self.save_sessions(originals, parameters).findings.get(item_index=0)
        return reverse(
            "phone:instance_summary",
            args=(self.case.pk, self.person.pk, self.person.pk, finding.pk),
        )

    @property
    def bulk_url(self):
        return reverse(
            "phone:instance_summaries",
            args=(self.case.pk, self.person.pk, self.person.pk),
        )

    def summary_payload(self, review):
        return {
            "submission_id": review.context["form"]["submission_id"].value(),
            "reviewed_scope": review.context["reviewed_scope"],
        }

    def select_external_ai(self):
        external_grant_kwargs(self.case, "session_summaries")
        self.case.external_ai_enabled = True
        self.case.save(update_fields=["external_ai_enabled"])

    def test_review_locks_members_and_adjustments_and_submit_replays_once(self):
        a, b, c = [self.message(n) for n in (1, 120, 360)]
        b_id = source_id({"id": b.pk, "kind": "text-message"})
        c_id = source_id({"id": c.pk, "kind": "text-message"})
        url = self.session(
            a,
            b,
            c,
            parameters={
                "gap_minutes": 20,
                "join_before": [b_id],
                "break_before": [c_id],
            },
        )
        self.login("editor")
        review = self.client.get(url, {"list_page": 2})
        self.assertContains(review, a.title)
        self.assertContains(review, b.title)
        self.assertNotContains(review, c.title)
        self.assertIn("list_page=2", review.context["back_url"])
        summaries = AnalysisRun.objects.filter(operation="session_summaries")
        self.assertFalse(summaries.exists())
        payload = self.summary_payload(review) | {
            "backend": "gateway",
            "sources": [f"text-message:{c.pk}"],
            "gap_minutes": 100,
            "external_processing": "on",
        }
        response = self.client.post(url, payload)
        batch = CaseAnalysisBatch.objects.get(
            case=self.case,
            requested_by=self.users["editor"],
            submission_id=payload["submission_id"],
        )
        self.assertNotEqual(str(batch.pk), str(payload["submission_id"]))
        self.assertIn(f"batch={batch.pk}", response.url)
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(self.client.post(url, payload).url, response.url)
        run = summaries.get()
        self.assertEqual([s["evidence_id"] for s in run.inputs], [str(a.pk), str(b.pk)])
        self.assertEqual(run.parameters["gap_minutes"], 20)
        self.assertEqual(run.parameters["join_before"], [b_id])
        self.assertNotIn("break_before", run.parameters)
        self.assertTrue(run.external_processing)

    def test_instance_summary_queues_only_the_selected_approved_route(self):
        original = self.message(1)
        url = self.session(original)
        self.login("editor")
        review = self.client.get(url)
        self.assertContains(review, "Request session summary")
        self.assertContains(review, "External AI")
        self.assertNotContains(review, 'name="backend"')
        self.assertNotContains(review, 'name="external_processing"')
        self.assertEqual(
            self.client.post(url, self.summary_payload(review)).status_code, 302
        )
        batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        run = batch.runs.get()
        self.assertTrue(run.external_processing)
        self.assertEqual(run.parameters["_execution"]["backend"], "gateway")
        self.assertEqual(
            AnalysisRun.objects.filter(operation="session_summaries").count(), 1
        )

    def test_pending_exact_grant_survives_approval_for_the_same_destination(self):
        url = self.session(self.message(1))
        self.select_external_ai()
        self.login("editor")
        payload = self.summary_payload(self.client.get(url))
        submitted = self.client.post(url, payload)
        batch = CaseAnalysisBatch.objects.get()
        execution = batch.scope["executions"]["gateway"]
        pinned_revision = batch.scope["external_grant_revision"]
        approve_scopes(
            self.case, self.case.owner, [transfer_scope("session_summaries", execution)]
        )
        self.assertNotEqual(
            case_grant_revision(self.case, "session_summaries", execution),
            pinned_revision,
        )
        self.assertRedirects(
            self.client.get(url), submitted.url, fetch_redirect_response=False
        )
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        run = batch.runs.get()
        self.assertEqual(
            run.parameters["_external_approval"]["revision"], pinned_revision
        )
        detail = reverse("core:analysis:detail", args=(self.case.pk, run.pk))
        self.assertRedirects(
            self.client.post(url, payload | {"submission_id": uuid4()}),
            detail,
            fetch_redirect_response=False,
        )
        self.assertEqual(CaseAnalysisBatch.objects.count(), 1)

    def test_disabling_external_ai_fences_previously_queued_work(self):
        url = self.session(self.message(1))
        self.login("editor")
        payload = self.summary_payload(self.client.get(url))
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        batch = CaseAnalysisBatch.objects.get()
        self.case.external_ai_enabled = False
        self.case.save(update_fields=["external_ai_enabled"])
        prepare_case_analysis_batch(batch.pk)
        self.assertFalse(batch.runs.exists())

    def test_backend_change_requires_new_single_and_bulk_review(self):
        single = self.session(self.message(1))
        self.session(self.message(2))
        self.login("editor")
        payloads = {}
        for url in (single, self.bulk_url):
            review = self.client.get(url)
            self.assertContains(review, "External AI")
            payloads[url] = self.summary_payload(review)

        self.case.external_ai_revision = uuid4()
        self.case.save(update_fields=["external_ai_revision"])
        self.select_external_ai()
        for url, payload in payloads.items():
            with self.subTest(url=url):
                updated = self.client.post(url, payload)
                self.assertContains(
                    updated, "Review the updated selection", status_code=409
                )
                self.assertContains(updated, "External AI", status_code=409)
        self.assertFalse(CaseAnalysisBatch.objects.exists())

        review = self.client.get(self.bulk_url)
        submitted = self.client.post(self.bulk_url, self.summary_payload(review))
        self.assertEqual(submitted.status_code, 302)
        batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertCountEqual(
            batch.runs.values_list("parameters__backend", flat=True),
            ["gateway", "gateway"],
        )

    def test_unapproved_selected_backend_has_no_private_fallback(self):
        original = self.message(1)
        single = self.session(original)
        self.case.external_analysis_approval = {}
        self.case.save(update_fields=["external_analysis_approval"])
        self.case.external_ai_enabled = True
        self.case.save(update_fields=["external_ai_enabled"])
        self.login("editor")
        for url in (single, self.bulk_url):
            with self.subTest(url=url):
                review = self.client.get(url)
                self.assertContains(
                    review,
                    "This analysis needs a destination approved in Case settings.",
                )
                self.assertNotContains(review, "No available sessions need a summary")
                self.assertNotContains(review, 'name="submission_id"')
                response = self.client.post(
                    url,
                    {
                        "submission_id": uuid4(),
                        "reviewed_scope": review.context["reviewed_scope"],
                    },
                )
                self.assertEqual(response.status_code, 400)
        self.assertFalse(CaseAnalysisBatch.objects.exists())

    def test_oversized_or_unavailable_scope_cannot_be_queued(self):
        a = self.message(0, "A" * 16001)
        url = self.session(a)
        self.login("owner")
        response = self.client.get(url)
        self.assertContains(response, "16,000-character")
        self.assertNotContains(response, ">Request summary</button>")
        payload = {"submission_id": uuid4(), "backend": "gateway"}
        self.assertEqual(self.client.post(url, payload).status_code, 400)
        TextMessageEvidence.objects.filter(pk=a.pk).update(sha256="0" * 64)
        self.assertEqual(self.client.post(url, payload).status_code, 404)
        self.assertFalse(
            AnalysisRun.objects.filter(operation="session_summaries").exists()
        )

    def test_summary_saved_during_review_returns_to_the_instance(self):
        original = self.message(8)
        url = self.session(original)
        self.login("owner")
        review = self.client.get(url)
        self.save_summary(original)
        response = self.client.post(url, self.summary_payload(review))
        self.assertRedirects(
            response, review.context["back_url"], fetch_redirect_response=False
        )
        self.assertEqual(
            AnalysisRun.objects.filter(operation="session_summaries").count(), 1
        )

    def save_summary(self, original):
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": original.pk}]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.case.owner,
            operation="session_summaries",
            method_version=get_operation("session_summaries").version,
            status="complete",
            external_processing=True,
            parameters={
                "backend": "gateway",
                "_execution": snapshot_execution(
                    "session_summaries", {"backend": "gateway"}, case=self.case
                ),
            },
            inputs=inputs,
        )
        source = inputs[0]["id"]
        AnalysisResult.objects.create(
            run=run,
            case=self.case,
            data={
                "items": [
                    {
                        "label": "Summary",
                        "text": "A synthetic topic.",
                        "source_ids": [source],
                        "values": {
                            "status": "suggestion",
                            "inferred_session": 1,
                            "chunk_source_ids": [source],
                        },
                    }
                ]
            },
        )

    def test_bulk_prepares_exact_missing_instances(self):
        originals = [self.message(n * 10) for n in range(26)]
        self.save_sessions(originals, {"gap_minutes": 1})
        self.session(*originals[-2:])
        self.session(self.message(1000, "A" * 16_001))
        self.save_summary(originals[0])
        self.login("editor")
        review = self.client.get(self.bulk_url, {"page": 2, "list_page": 3})
        self.assertContains(review, "Sessions included · 26")
        self.assertEqual(len(review.context["instances"]), 26)
        self.assertContains(review, "1 session exceeds the message or text limit")
        self.assertIn("page=2", review.context["back_url"])
        self.assertIn("list_page=3", review.context["back_url"])
        self.assertFalse(CaseAnalysisBatch.objects.exists())

        payload = self.summary_payload(review) | {"sources": [str(originals[0].pk)]}
        submitted = self.client.post(self.bulk_url, payload)
        self.assertEqual(submitted.status_code, 302)
        self.assertContains(
            self.client.get(submitted.url), "Back to sessions &amp; summaries"
        )
        self.assertRedirects(self.client.post(self.bulk_url, payload), submitted.url)
        self.assertRedirects(
            self.client.post(self.bulk_url, payload | {"submission_id": uuid4()}),
            submitted.url,
        )
        batch = CaseAnalysisBatch.objects.get()
        self.assertFalse(batch.runs.exists())
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
        self.assertEqual(batch.runs.count(), 10)
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertCountEqual(
            [
                [source["evidence_id"] for source in run.inputs]
                for run in batch.runs.all()
            ],
            [[str(message.pk)] for message in originals[1:]]
            + [[str(message.pk) for message in originals[-2:]]],
        )
        self.assertFalse(batch.runs.filter(external_processing=False).exists())

    def test_bulk_review_queues_only_the_current_review_page(self):
        originals = [self.message(n * 10) for n in range(51)]
        self.save_sessions(originals, {"gap_minutes": 1})
        self.login("owner")
        first = self.client.get(self.bulk_url)
        second_url = self.bulk_url + "?review_page=2"
        second = self.client.get(second_url)
        self.assertContains(first, "Sessions included · 50")
        self.assertContains(second, "Sessions included · 1")
        self.assertContains(second, "Page 2 of 2")
        mismatched = self.client.post(second_url, self.summary_payload(first))
        self.assertContains(mismatched, "Review the updated selection", status_code=409)
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        self.assertEqual(
            self.client.post(second_url, self.summary_payload(second)).status_code, 302
        )
        batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(
            batch.runs.get().inputs[0]["evidence_id"], str(originals[-1].pk)
        )

    def test_single_and_bulk_reuse_exact_pending_work(self):
        a, b = self.message(1), self.message(2)
        single = self.session(a)
        overlapping = self.session(a, b)
        self.login("editor")
        first_review = self.client.get(single)
        overlapping_review = self.client.get(overlapping)
        first_payload = self.summary_payload(first_review)
        original_request = self.client.post(single, first_payload)
        self.assertEqual(original_request.status_code, 302)
        self.assertRedirects(self.client.get(single), original_request.url)
        self.assertRedirects(
            self.client.post(single, first_payload), original_request.url
        )
        first_batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(prepare_case_analysis_batch(first_batch.pk), "complete")
        bulk = self.client.get(self.bulk_url)
        self.assertContains(bulk, "1 summary already in progress")
        self.assertContains(bulk, "Sessions included · 1")
        bulk_request = self.client.post(self.bulk_url, self.summary_payload(bulk))
        self.assertEqual(bulk_request.status_code, 302)
        # Even a single summary reviewed before the bulk request joins the batch.
        self.assertRedirects(
            self.client.post(overlapping, self.summary_payload(overlapping_review)),
            bulk_request.url,
        )
        batch = CaseAnalysisBatch.objects.exclude(pk=first_batch.pk).get()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        batch.refresh_from_db()
        self.assertEqual(batch.queued_runs, 1)
        self.assertCountEqual(
            [source["evidence_id"] for source in batch.runs.get().inputs],
            [str(a.pk), str(b.pk)],
        )
        self.assertEqual(
            AnalysisRun.objects.filter(operation="session_summaries").count(), 2
        )

    def test_bulk_worker_skips_exact_work_started_after_review(self):
        original = self.message(1)
        self.session(original)
        self.login("owner")
        payload = self.summary_payload(self.client.get(self.bulk_url))
        self.assertEqual(self.client.post(self.bulk_url, payload).status_code, 302)
        start_run(
            self.case.owner,
            self.case.pk,
            "session_summaries",
            [{"kind": "text-message", "id": str(original.pk)}],
            parameters={"backend": "gateway"},
            **external_grant_kwargs(self.case, "session_summaries"),
        )
        batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        batch.refresh_from_db()
        self.assertEqual((batch.processed_sources, batch.queued_runs), (1, 0))
        self.assertFalse(batch.runs.exists())

    def test_pending_run_with_different_grouping_does_not_reserve_session(self):
        originals = [self.message(minute) for minute in (1, 61)]
        url = self.session(*originals, parameters={"gap_minutes": 120})
        start_run(
            self.case.owner,
            self.case.pk,
            "session_summaries",
            [{"kind": "text-message", "id": str(item.pk)} for item in originals],
            parameters={"backend": "gateway", "gap_minutes": 10},
            **external_grant_kwargs(self.case, "session_summaries"),
        )
        self.login("owner")
        self.assertContains(self.client.get(self.bulk_url), "Sessions included · 1")
        review = self.client.get(url)
        self.assertContains(review, "Request session summary")
        self.assertEqual(
            self.client.post(url, self.summary_payload(review)).status_code, 302
        )
        prepare_case_analysis_batch(CaseAnalysisBatch.objects.get().pk)
        self.assertCountEqual(
            AnalysisRun.objects.filter(operation="session_summaries").values_list(
                "parameters__gap_minutes", flat=True
            ),
            [10, 120],
        )

    @override_settings(
        PANDECT_AI_GATEWAY=TRANSCRIPTION_CONFIG
        | {
            "account": "",
            "gateway": "",
            "gateway_token": "",
            "workers_token": "",
        },
    )
    def test_unconfigured_backend_cannot_queue_single_or_bulk_summaries(self):
        single = self.session(self.message(1))
        self.login("owner")
        for url in (single, self.bulk_url):
            with self.subTest(url=url):
                review = self.client.get(url)
                self.assertContains(
                    review, "A service for this analysis is not configured."
                )
                payload = {
                    "submission_id": uuid4(),
                    "reviewed_scope": review.context["reviewed_scope"],
                }
                rejected = self.client.post(url, payload)
                self.assertContains(
                    rejected,
                    "A service for this analysis is not configured.",
                    status_code=400,
                )
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        self.assertFalse(
            AnalysisRun.objects.filter(operation="session_summaries").exists()
        )

    def test_bulk_rechecks_membership_and_keeps_unchanged_legacy_routes(self):
        self.session(self.message(1))
        external_grant_kwargs(self.case, "session_summaries")
        self.login("owner")
        review = self.client.get(self.bulk_url)
        payload = self.summary_payload(review)
        self.session(self.message(2))
        changed = self.client.post(self.bulk_url, payload)
        self.assertContains(changed, "Review the updated selection", status_code=409)
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        payload = self.summary_payload(changed)
        submitted = self.client.post(self.bulk_url, payload)
        self.assertEqual(submitted.status_code, 302)
        batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(set(batch.scope["executions"]), {"gateway"})
        self.assertRedirects(self.client.get(self.bulk_url), submitted.url)
        gateway = settings.PANDECT_AI_GATEWAY | {"gateway": "changed-synthetic-route"}
        with self.settings(PANDECT_AI_GATEWAY=gateway):
            prepare_case_analysis_batch(batch.pk)
        self.assertFalse(batch.runs.exists())
        self.assertContains(self.client.get(self.bulk_url), "Sessions included · 2")

    def test_bulk_preserves_other_runs_when_a_reviewed_instance_becomes_unavailable(
        self,
    ):
        original = self.message(1)
        self.session(original)
        other = self.message(2)
        self.session(other)
        self.login("editor")
        payload = self.summary_payload(self.client.get(self.bulk_url))
        self.assertEqual(self.client.post(self.bulk_url, payload).status_code, 302)
        batch = CaseAnalysisBatch.objects.get()
        TextMessageEvidence.objects.filter(pk=original.pk).update(sha256="0" * 64)
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        batch.refresh_from_db()
        self.assertEqual(batch.unavailable_sources, 1)
        self.assertEqual(batch.runs.get().inputs[0]["evidence_id"], str(other.pk))

    def test_bulk_worker_stops_when_the_requester_loses_access(self):
        self.session(self.message(1))
        self.login("editor")
        payload = self.summary_payload(self.client.get(self.bulk_url))
        self.assertEqual(self.client.post(self.bulk_url, payload).status_code, 302)
        batch = CaseAnalysisBatch.objects.get()
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "access_removed")
        self.assertFalse(batch.runs.exists())

    def test_failed_summary_can_retry_independently_in_its_batch(self):
        first, second = self.message(1), self.message(2)
        self.session(first)
        self.session(second)
        self.login("editor")
        payload = self.summary_payload(self.client.get(self.bulk_url))
        self.assertEqual(self.client.post(self.bulk_url, payload).status_code, 302)
        batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        failed, succeeding = [
            batch.runs.get(inputs__contains=[{"evidence_id": str(message.pk)}])
            for message in (first, second)
        ]
        with (
            patch(
                "core.analysis.gateway.GatewayClient.infer",
                side_effect=AnalysisError("gateway_unavailable"),
            ),
        ):
            self.assertEqual(process_run(failed.pk), "gateway_unavailable")
        self.assertFalse(AnalysisResult.objects.filter(run=failed).exists())
        response = self.client.post(
            reverse("core:analysis:retry", args=(self.case.pk, failed.pk)),
            {"submission_id": uuid4()},
        )
        self.assertEqual(response.status_code, 302)
        retried = batch.runs.get(previous_run=failed)
        for run, original in ((succeeding, second), (retried, first)):
            with patch(
                "core.analysis.gateway.GatewayClient.infer",
                return_value={
                    "items": [
                        {
                            "text": (
                                f"The reported sender {self.person.pk} "
                                "discusses a synthetic topic."
                            ),
                            "citations": [
                                {
                                    "source_id": run.inputs[0]["id"],
                                    "quote": original.body,
                                }
                            ],
                        }
                    ]
                },
            ):
                process_run(run.pk)
            self.assertTrue(AnalysisResult.objects.filter(run=run).exists())
        report = self.client.get(
            reverse("core:analysis:detail", args=(self.case.pk, retried.pk))
        )
        self.assertContains(report, f"{self.person} discusses a synthetic topic.")
        self.assertNotContains(report, f"reported sender {self.person}")
        self.assertContains(
            self.client.get(self.bulk_url), "No available sessions need a summary."
        )

    def test_viewers_revoked_members_and_other_cases_cannot_start(self):
        url = self.session(self.message(1))
        payload = {"submission_id": uuid4(), "backend": "gateway"}
        for role, status in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            for target in (url, self.bulk_url):
                self.assertEqual(self.client.get(target).status_code, status)
                self.assertEqual(self.client.post(target, payload).status_code, status)
        self.login("editor")
        self.assertEqual(self.client.get(url).status_code, 200)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.client.post(url, payload).status_code, 404)
        self.assertFalse(
            AnalysisRun.objects.filter(operation="session_summaries").exists()
        )

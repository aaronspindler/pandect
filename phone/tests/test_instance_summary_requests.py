"""Exact saved-session summary requests through the investigator workflow."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.catalogue import project_result
from core.analysis.operations import get_operation
from core.analysis.services import resolve_inputs, snapshot_sources
from core.analysis.sources import source_id
from core.models import AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from phone.models import TextMessageEvidence


class InstanceSummaryRequestTests(EvidenceViewTestMixin, TestCase):
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

    def session(self, *originals, parameters=None):
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
        finding = result.findings.get(item_index=0)
        return reverse(
            "phone:instance_summary",
            args=(self.case.pk, self.person.pk, self.person.pk, finding.pk),
        )

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
        payload = {
            "backend": "local",
            "submission_id": review.context["form"]["submission_id"].value(),
            "sources": [f"text-message:{c.pk}"],
            "gap_minutes": 100,
            "external_processing": "on",
        }
        for _ in range(2):
            response = self.client.post(url, payload)
            run = summaries.get()
            self.assertRedirects(
                response,
                reverse("core:analysis:detail", args=(self.case.pk, run.pk)),
                fetch_redirect_response=False,
            )
        self.assertEqual([s["evidence_id"] for s in run.inputs], [str(a.pk), str(b.pk)])
        self.assertEqual(run.parameters["gap_minutes"], 20)
        self.assertEqual(run.parameters["join_before"], [b_id])
        self.assertNotIn("break_before", run.parameters)
        self.assertFalse(run.external_processing)

    def test_gateway_approval_binds_actor_membership_and_destination(self):
        url = self.session(self.message(1))
        second = self.session(self.message(2))
        self.login("owner")
        review = self.client.get(url)
        form = review.context["form"]
        payload = {
            "backend": "gateway",
            "submission_id": form["submission_id"].value(),
            "execution_confirmation": form["execution_confirmation"].value(),
        }
        self.assertEqual(self.client.post(url, payload).status_code, 400)
        payload["external_processing"] = "on"
        self.assertEqual(self.client.post(second, payload).status_code, 400)
        self.login("editor")
        self.assertEqual(self.client.post(url, payload).status_code, 400)
        self.login("owner")
        gateway = settings.PANDECT_AI_GATEWAY | {"gateway": "changed-synthetic-route"}
        with self.settings(PANDECT_AI_GATEWAY=gateway):
            changed = self.client.post(url, payload)
        self.assertContains(changed, "Review the destination", status_code=400)
        self.assertFalse(
            AnalysisRun.objects.filter(operation="session_summaries").exists()
        )
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        run = AnalysisRun.objects.get(operation="session_summaries")
        self.assertTrue(run.external_processing)
        self.assertEqual(run.parameters["_execution"]["backend"], "gateway")

    def test_oversized_or_unavailable_scope_cannot_be_queued(self):
        a = self.message(0, "A" * 16001)
        url = self.session(a)
        self.login("owner")
        response = self.client.get(url)
        self.assertContains(response, "16,000-character")
        self.assertNotContains(response, ">Request summary</button>")
        payload = {"submission_id": uuid4(), "backend": "local"}
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
        inputs = snapshot_sources(
            self.case, [{"kind": "text-message", "id": original.pk}]
        )
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.case.owner,
            operation="session_summaries",
            method_version=get_operation("session_summaries").version,
            status="complete",
            parameters={"backend": "local"},
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
        response = self.client.post(
            url,
            {
                "backend": "local",
                "submission_id": review.context["form"]["submission_id"].value(),
            },
        )
        self.assertRedirects(
            response, review.context["back_url"], fetch_redirect_response=False
        )
        self.assertEqual(
            AnalysisRun.objects.filter(operation="session_summaries").count(), 1
        )

    def test_viewers_revoked_members_and_other_cases_cannot_start(self):
        url = self.session(self.message(1))
        payload = {"submission_id": uuid4(), "backend": "local"}
        for role, status in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(self.client.get(url).status_code, status)
            self.assertEqual(self.client.post(url, payload).status_code, status)
        self.login("editor")
        self.assertEqual(self.client.get(url).status_code, 200)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(self.client.post(url, payload).status_code, 404)
        self.assertFalse(
            AnalysisRun.objects.filter(operation="session_summaries").exists()
        )

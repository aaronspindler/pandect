"""Case-authorized bulk selections with case-wide route approval."""

from django.test import TestCase, override_settings
from django.urls import reverse
from django_tasks_db.models import DBTaskResult

from cases.models import Case, CaseMembership
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_gateway import CONFIG
from core.models import AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence


class ReprocessingViewTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = self.text()
        self.location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic observation",
            address="Synthetic harbour",
        )

    def reprocess_url(self, case=None):
        return reverse("core:analysis:reprocess", args=[(case or self.case).pk])

    def selection(self, role="owner", case=None, **overrides):
        self.login(role)
        response = self.client.get(self.reprocess_url(case))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        return {
            "submission_id": str(form["submission_id"].value()),
            "evidence_types": ["text", "location"],
        } | overrides

    def test_processing_links_to_read_only_selection_screen(self):
        self.login("owner")
        before = (
            CaseAnalysisBatch.objects.count(),
            AnalysisRun.objects.count(),
            DBTaskResult.objects.count(),
        )
        processing = self.client.get(
            reverse("core:analysis:processing", args=[self.case.pk])
        )
        self.assertContains(processing, self.reprocess_url())
        response = self.client.get(self.reprocess_url())
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(set(form["evidence_types"].value()), {"text", "location"})
        self.assertEqual(set(form.fields), {"evidence_types", "submission_id"})
        self.assertEqual(
            (
                CaseAnalysisBatch.objects.count(),
                AnalysisRun.objects.count(),
                DBTaskResult.objects.count(),
            ),
            before,
        )

    def test_empty_case_has_no_default_selection_and_cannot_submit(self):
        self.login("outsider")
        response = self.client.get(self.reprocess_url(self.other_case))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["evidence_types"].value(), [])
        self.assertEqual(response.context["active_count"], 0)
        self.assertContains(response, "No active evidence is available.")
        self.assertContains(
            response,
            '<button class="button button-primary" type="submit" disabled>'
            "Start reprocessing</button>",
            html=True,
        )
        response = self.client.post(
            self.reprocess_url(self.other_case),
            {"submission_id": str(response.context["form"]["submission_id"].value())},
        )
        self.assertContains(response, 'href="#id_evidence_types_group"')
        self.assertContains(response, 'id="id_evidence_types_group"')
        self.assertContains(response, 'id="id_evidence_types_error"')
        self.assertFalse(CaseAnalysisBatch.objects.exists())

    def test_owner_and_editor_submit_selected_types_and_duplicate_returns_same_batch(
        self,
    ):
        for role in ("owner", "editor"):
            with self.subTest(role=role):
                payload = self.selection(role, evidence_types=["text"])
                response = self.client.post(self.reprocess_url(), payload)
                batch = CaseAnalysisBatch.objects.get(pk=payload["submission_id"])
                self.assertEqual(batch.case, self.case)
                self.assertEqual(batch.requested_by, self.users[role])
                self.assertEqual(batch.scope["kinds"], ["text"])
                self.assertEqual(batch.status, "queued")
                target = (
                    reverse("core:analysis:processing", args=[self.case.pk])
                    + f"?batch={batch.pk}#processing-history"
                )
                self.assertRedirects(response, target, fetch_redirect_response=False)
                tasks = DBTaskResult.objects.count()
                repeated = self.client.post(self.reprocess_url(), payload)
                self.assertRedirects(repeated, target, fetch_redirect_response=False)
                self.assertEqual(DBTaskResult.objects.count(), tasks)
                batch.status = CaseAnalysisBatch.Status.COMPLETE
                batch.save(update_fields=["status"])
        self.assertFalse(AnalysisRun.objects.exists())

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_external_routing_requires_case_selection_and_owner_approval(self):
        payload = self.selection(evidence_types=["text"])
        response = self.client.get(self.reprocess_url())
        self.assertNotContains(response, "Choose processing location")
        self.assertNotContains(response, 'name="external_processing"')
        accepted = self.client.post(
            self.reprocess_url(),
            payload | {"mode": "external", "external_processing": "on"},
        )
        self.assertEqual(accepted.status_code, 302)
        first = CaseAnalysisBatch.objects.get(pk=payload["submission_id"])
        self.assertNotIn("content_events:gateway", first.scope["executions"])
        first.status = CaseAnalysisBatch.Status.COMPLETE
        first.save(update_fields=["status"])

        approval = external_grant_kwargs(self.case, "content_events")
        next_payload = self.selection("editor", evidence_types=["text"])
        second = self.client.post(self.reprocess_url(), next_payload)
        self.assertEqual(second.status_code, 302)
        second_batch = CaseAnalysisBatch.objects.get(pk=next_payload["submission_id"])
        self.assertNotIn("content_events:gateway", second_batch.scope["executions"])

        self.case.ai_backend = Case.AIBackend.GATEWAY
        self.case.save(update_fields=("ai_backend",))
        external_payload = self.selection("editor", evidence_types=["text"])
        self.assertEqual(
            self.client.post(self.reprocess_url(), external_payload).status_code, 302
        )
        batch = CaseAnalysisBatch.objects.get(pk=external_payload["submission_id"])
        self.assertEqual(
            batch.scope["executions"]["content_events:gateway"],
            approval["approved_execution"],
        )
        self.assertEqual(
            batch.scope["external_grants"]["content_events"],
            approval["external_grant_revision"],
        )

    def test_invalid_selection_is_retained_without_queuing(self):
        for evidence_types in ([], ["text", "invented"]):
            with self.subTest(evidence_types=evidence_types):
                payload = self.selection(evidence_types=evidence_types)
                response = self.client.post(self.reprocess_url(), payload)
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertTrue(form.errors)
                self.assertEqual(form["evidence_types"].value(), evidence_types)
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())

    def test_evidence_type_checkboxes_describe_validation_errors(self):
        response = self.client.post(
            self.reprocess_url(), self.selection(evidence_types=[])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("evidence_types", response.context["form"].errors)
        self.assertContains(
            response,
            'id="id_evidence_types_group" '
            'aria-describedby="reprocess-types-help id_evidence_types_error"',
        )
        self.assertContains(response, 'id="id_evidence_types_error"')
        self.assertContains(response, 'href="#id_evidence_types_group"')

    def test_viewers_and_unrelated_or_revoked_users_cannot_reprocess(self):
        payload = self.selection("editor")
        for role, expected in (("viewer", 403), ("outsider", 404)):
            self.login(role)
            with self.subTest(role=role):
                self.assertEqual(
                    self.client.get(self.reprocess_url()).status_code, expected
                )
                self.assertEqual(
                    self.client.post(self.reprocess_url(), payload).status_code,
                    expected,
                )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.login("editor")
        self.assertEqual(self.client.get(self.reprocess_url()).status_code, 404)
        self.assertEqual(
            self.client.post(self.reprocess_url(), payload).status_code, 404
        )
        self.assertFalse(CaseAnalysisBatch.objects.exists())

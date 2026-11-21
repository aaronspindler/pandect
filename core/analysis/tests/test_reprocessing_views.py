"""Case-authorized bulk selections and explicit external approval."""

from uuid import uuid4

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django_tasks_db.models import DBTaskResult

from cases.models import CaseMembership
from core.analysis.tests.test_execution import LOCAL
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
            "execution_confirmation": form["execution_confirmation"].value(),
            "evidence_types": ["text", "location"],
            "mode": "local",
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
        self.assertEqual(form["mode"].value(), "local")
        self.assertFalse(form["external_processing"].value())
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
                self.assertEqual(batch.scope["mode"], "local")
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
        self.assertFalse(AnalysisRun.objects.exists())

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_external_and_both_require_deliberate_approval(self):
        for mode in ("external", "both"):
            with self.subTest(mode=mode):
                payload = self.selection(mode=mode)
                response = self.client.post(self.reprocess_url(), payload)
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertIn("external_processing", form.errors)
                self.assertEqual(form["mode"].value(), mode)
                self.assertEqual(form["evidence_types"].value(), ["text", "location"])
                self.assertFalse(
                    CaseAnalysisBatch.objects.filter(
                        pk=payload["submission_id"]
                    ).exists()
                )
                payload["external_processing"] = "on"
                approved = self.client.post(self.reprocess_url(), payload)
                self.assertEqual(approved.status_code, 302)
                batch = CaseAnalysisBatch.objects.get(pk=payload["submission_id"])
                self.assertEqual(batch.scope["mode"], mode)

    def test_invalid_selection_is_retained_without_queuing(self):
        for invalid in (
            {"evidence_types": []},
            {"evidence_types": ["text", "invented"]},
            {"mode": "unapproved-service"},
        ):
            with self.subTest(invalid=invalid):
                payload = self.selection(**invalid)
                response = self.client.post(self.reprocess_url(), payload)
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertTrue(form.errors)
                self.assertEqual(form["mode"].value(), payload["mode"])
                self.assertEqual(
                    form["evidence_types"].value(), payload["evidence_types"]
                )
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_checkbox_and_radio_groups_describe_their_validation_errors(self):
        payload = self.selection(evidence_types=[], mode="unapproved-service")
        response = self.client.post(self.reprocess_url(), payload)
        self.assertEqual(response.status_code, 200)
        for name, help_id in (
            ("evidence_types", "reprocess-types-help"),
            ("mode", "reprocess-mode-help"),
        ):
            with self.subTest(field=name):
                self.assertIn(name, response.context["form"].errors)
                self.assertContains(
                    response,
                    f'id="id_{name}" aria-describedby="{help_id} id_{name}_error"',
                )
                self.assertContains(response, f'id="id_{name}_error"')
                self.assertContains(response, f'href="#id_{name}"')
        self.assertFalse(CaseAnalysisBatch.objects.exists())

    def test_confirmation_cannot_be_tampered_rebound_or_reused_by_another_editor(self):
        original = self.selection()
        for changed in (
            {"execution_confirmation": "invalid-token"},
            {"submission_id": str(uuid4())},
        ):
            with self.subTest(changed=changed):
                response = self.client.post(self.reprocess_url(), original | changed)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].errors)
        self.login("editor")
        response = self.client.post(self.reprocess_url(), original)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.login("outsider")
        cross_case = self.client.post(self.reprocess_url(self.other_case), original)
        self.assertEqual(cross_case.status_code, 200)
        self.assertTrue(cross_case.context["form"].errors)
        self.assertFalse(CaseAnalysisBatch.objects.exists())

    @override_settings(PANDECT_LOCAL_AI=LOCAL)
    def test_changed_processing_destination_requires_a_fresh_review(self):
        payload = self.selection()
        configuration = settings.PANDECT_LOCAL_AI
        with self.settings(
            PANDECT_LOCAL_AI=configuration
            | {"text": configuration.get("text", {}) | {"runtime": "changed-runtime"}}
        ):
            response = self.client.post(self.reprocess_url(), payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(response.context["form"]["mode"].value(), "local")
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())

    @override_settings(PANDECT_LOCAL_AI={})
    def test_unconfigured_network_modes_are_not_offered_or_queued(self):
        payload = self.selection(evidence_types=["text"])
        response = self.client.get(self.reprocess_url())
        self.assertEqual(
            list(response.context["form"].fields["mode"].choices),
            [("local", "Local checks and private services")],
        )
        self.assertContains(response, 'type="hidden" name="mode" value="local"')
        self.assertContains(response, "Processing uses local checks")
        self.assertNotContains(response, "Choose processing location")
        self.assertNotContains(response, "0 active")
        self.assertNotContains(response, "Cloudflare")
        rejected = self.client.post(
            self.reprocess_url(),
            payload | {"mode": "external", "external_processing": "on"},
        )
        self.assertIn("mode", rejected.context["form"].errors)
        accepted = self.client.post(self.reprocess_url(), payload)
        self.assertEqual(accepted.status_code, 302)
        batch = CaseAnalysisBatch.objects.get(pk=payload["submission_id"])
        self.assertEqual(batch.scope["executions"], {})

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

"""Deliberate analysis requires the current case approval for model work."""

from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from core.analysis.errors import AnalysisError
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_gateway import TRANSCRIPTION_CONFIG
from core.analysis.worker import process_run
from core.models import AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin


class AnalysisRoutingFormTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = self.text()
        self.url = reverse("core:analysis:create", args=(self.case.pk,))
        self.login("editor")

    def analysis(self, operation):
        page = self.client.get(self.url, {"operation": operation})
        self.assertNotContains(page, 'name="backend"')
        self.assertNotContains(page, 'name="external_processing"')
        self.assertNotContains(page, 'name="execution_confirmation"')
        return {
            "operation": operation,
            "submission_id": page.context["form"]["submission_id"].value(),
            "sources": [f"text:{self.note.pk}"],
        }

    @override_settings(PANDECT_AI_GATEWAY=TRANSCRIPTION_CONFIG)
    def test_topics_queues_keywords_once_and_uses_only_gateway_for_suggestions(self):
        external_grant_kwargs(self.case, "text_topics")
        payload = self.analysis("text_topics") | {"include_summary": ""}
        response = self.client.post(self.url, payload)
        batch = CaseAnalysisBatch.objects.get()
        self.assertIn(f"batch={batch.pk}", response.url)
        runs = batch.runs.all()
        self.assertEqual(runs.count(), 2)
        keywords = runs.get(parameters__include_summary=False)
        self.assertFalse(keywords.external_processing)
        suggestions = runs.filter(
            parameters__include_summary=True, parameters___suggestions_only=True
        )
        self.assertEqual(
            set(suggestions.values_list("parameters__backend", flat=True)),
            {"gateway"},
        )
        self.assertEqual(self.client.post(self.url, payload).url, response.url)
        self.assertEqual(AnalysisRun.objects.count(), 2)
        process_run(keywords.pk)
        client = Mock()
        client.infer.side_effect = AnalysisError("gateway_unavailable")
        suggested_run = suggestions.get(parameters__backend="gateway")
        with (
            patch("core.analysis.gateway.GatewayClient", return_value=client),
            patch("text.analysis.ai.keywords") as repeated_keywords,
        ):
            self.assertEqual(process_run(suggested_run.pk), "gateway_unavailable")
            repeated_keywords.assert_not_called()
        self.assertTrue(hasattr(keywords, "result"))
        self.assertFalse(hasattr(suggested_run, "result"))

    @override_settings(PANDECT_AI_GATEWAY=TRANSCRIPTION_CONFIG)
    def test_selected_external_ai_runs_once_and_requires_approval(self):
        payload = self.analysis("content_events") | {"backend": "local"}
        response = self.client.post(self.url, payload)
        self.assertContains(response, "Enable external AI")
        self.assertFalse(AnalysisRun.objects.exists())

        external_grant_kwargs(self.case, "content_events")
        self.assertEqual(self.client.post(self.url, payload).status_code, 302)
        run = AnalysisRun.objects.get()
        self.assertEqual(run.parameters["backend"], "gateway")
        self.assertTrue(run.external_processing)

    def test_optional_model_without_service_leaves_keywords_available(self):
        payload = self.analysis("text_topics") | {"include_summary": "on"}
        self.assertEqual(self.client.post(self.url, payload).status_code, 302)
        self.assertFalse(AnalysisRun.objects.get().parameters["include_summary"])

    def test_retained_history_shows_route_without_exposing_private_endpoint(self):
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version="1",
            parameters={
                "backend": "local",
                "_execution": {
                    "backend": "local",
                    "location": "local",
                    "model": "Synthetic local model",
                    "runtime": "synthetic-runtime-revision",
                    "url": "http://private-inference.example.test:9000",
                },
            },
            inputs=[
                {
                    "id": f"text:{self.note.pk}",
                    "kind": "text",
                    "evidence_id": str(self.note.pk),
                    "sha256": self.note.sha256,
                }
            ],
        )
        self.login("viewer")
        response = self.client.get(
            reverse("core:analysis:detail", args=(self.case.pk, run.pk))
        )
        self.assertContains(response, "Local processing")
        self.assertContains(response, "Synthetic local model")
        self.assertContains(response, "synthetic-runtime-revision")
        self.assertNotContains(response, "private-inference.example.test")
        self.assertNotContains(response, "predates execution pinning")
        self.assertNotContains(response, "Start another run")

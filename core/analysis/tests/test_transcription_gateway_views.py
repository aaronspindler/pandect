"""Submission and recovery when Workers AI transcription is unavailable."""

from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django_tasks_db.models import DBTaskResult

from audio.models import AudioEvidence
from core.analysis.services import process_run
from core.analysis.testing import wav_bytes
from core.analysis.tests.test_gateway import CONFIG, response
from core.analysis.tests.test_openai_gateway import OPENAI
from core.models import AnalysisRun, GatewayRequest
from core.testing import EvidenceViewTestMixin

OPENAI_ONLY = OPENAI | {"max_request_bytes": 65536}
TRANSCRIPTION_GATEWAY = OPENAI_ONLY | {"workers_token": CONFIG["workers_token"]}
UNAVAILABLE = "Ask the operator to configure Workers AI for recordings."


@override_settings(PANDECT_AI_GATEWAY=OPENAI_ONLY)
class TranscriptionGatewayViewTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.recording = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic recording",
            file=ContentFile(wav_bytes(), name="recording.wav"),
        )
        self.source = f"audio:{self.recording.pk}"
        self.create_url = reverse("core:analysis:create", args=[self.case.pk])
        self.login("editor")

    def reviewed_create(self):
        page = self.client.get(self.create_url, {"operation": "transcription"})
        form = page.context["form"]
        return page, {
            "operation": "transcription",
            "submission_id": form["submission_id"].value(),
            "execution_confirmation": form["execution_confirmation"].value(),
            "sources": [self.source],
            "backend": "gateway",
            "external_processing": "on",
        }

    def reviewed_retry(self, detail_url):
        page = self.client.get(detail_url)
        self.assertContains(page, "Workers AI via Cloudflare AI Gateway")
        return {
            "submission_id": page.context["retry_submission_id"],
            "retry_confirmation": page.context["retry_confirmation"],
            "external_processing": "on",
        }

    def test_openai_text_configuration_cannot_queue_external_transcription(self):
        page, payload = self.reviewed_create()
        rejected = self.client.post(self.create_url, payload)
        self.assertEqual(rejected.status_code, 200)
        self.assertContains(rejected, UNAVAILABLE)
        self.assertContains(page, UNAVAILABLE)
        self.assertEqual(rejected.context["form"]["sources"].value(), [self.source])
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())

    def test_retry_is_blocked_until_workers_configuration_is_restored(self):
        with self.settings(PANDECT_AI_GATEWAY=TRANSCRIPTION_GATEWAY):
            _, payload = self.reviewed_create()
            queued = self.client.post(self.create_url, payload)
        original = AnalysisRun.objects.get()
        detail_url = reverse("core:analysis:detail", args=[self.case.pk, original.pk])
        retry_url = reverse("core:analysis:retry", args=[self.case.pk, original.pk])
        self.assertRedirects(queued, detail_url, fetch_redirect_response=False)
        repeated = self.client.post(self.create_url, payload)
        self.assertRedirects(repeated, detail_url, fetch_redirect_response=False)
        self.assertEqual(DBTaskResult.objects.count(), 1)

        with patch("core.analysis.gateway.build_opener") as build:
            process_run(original.pk)
        build.assert_not_called()
        original.refresh_from_db()
        self.assertEqual(original.status, AnalysisRun.Status.FAILED)
        self.assertEqual(original.error_code, "gateway_not_configured")

        with self.settings(PANDECT_AI_GATEWAY=TRANSCRIPTION_GATEWAY):
            reviewed = self.reviewed_retry(detail_url)
        tasks_before = DBTaskResult.objects.count()
        blocked = self.client.get(detail_url)
        self.assertContains(blocked, UNAVAILABLE)
        self.assertNotContains(blocked, "Start another run with these inputs")
        rejected = self.client.post(retry_url, reviewed, follow=True)
        self.assertRedirects(rejected, detail_url)
        self.assertContains(rejected, UNAVAILABLE)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(DBTaskResult.objects.count(), tasks_before)
        self.assertFalse(GatewayRequest.objects.exists())

        with self.settings(PANDECT_AI_GATEWAY=TRANSCRIPTION_GATEWAY):
            payload = self.reviewed_retry(detail_url)
            retried = self.client.post(retry_url, payload)
            rerun = AnalysisRun.objects.get(previous_run=original)
            self.assertRedirects(
                retried,
                reverse("core:analysis:detail", args=[self.case.pk, rerun.pk]),
                fetch_redirect_response=False,
            )
            self.assertEqual(rerun.inputs, original.inputs)
            self.assertTrue(rerun.external_processing)
            self.assertEqual(DBTaskResult.objects.count(), tasks_before + 1)
            with patch("core.analysis.gateway.build_opener") as build:
                words = "Synthetic spoken words"
                build.return_value.open.return_value = response(
                    {
                        "result": {
                            "text": words,
                            "segments": [{"start": 0, "end": 1, "text": words}],
                        }
                    }
                )
                process_run(rerun.pk)
        rerun.refresh_from_db()
        self.assertEqual(rerun.status, AnalysisRun.Status.COMPLETE, rerun.error_code)
        self.assertEqual(GatewayRequest.objects.get(run=rerun).status, "complete")
        self.assertEqual(rerun.result.passages.get().text, words)

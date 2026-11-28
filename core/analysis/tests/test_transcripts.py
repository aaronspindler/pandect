from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from audio.models import AudioEvidence
from core.analysis.automatic import start_added_evidence_analysis
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.services import cancel_run
from core.analysis.tests.test_gateway import TRANSCRIPTION_CONFIG
from core.analysis.tests.testing import external_grant_kwargs, wav_bytes
from core.analysis.transcripts import (
    follow_up_transcription,
    select_transcript,
)
from core.analysis.worker import process_run
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch
from core.tests.testing import EvidenceViewTestMixin


class AutomaticTranscriptTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.batch = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.users["owner"], scope={"type": "added"}
        )

    def attempt(
        self, backend, status, *, group="recording", model="synthetic", text=""
    ):
        run = AnalysisRun.objects.create(
            batch=self.batch,
            case=self.case,
            requested_by=self.users["owner"],
            operation="transcription",
            method_version="1",
            parameters={
                "_transcript_group": group,
                "_execution": {"backend": backend, "model": model},
            },
            status=status,
        )
        if status in ("complete", "partial"):
            AnalysisResult.objects.create(
                case=self.case, run=run, data={"items": [{"text": text}]}
            )
        return run

    def retain_follow_up(self, original, result):
        AnalysisRun.objects.create(
            batch=self.batch,
            case=self.case,
            requested_by=self.users["owner"],
            operation="entities",
            method_version="1",
            previous_run=original,
            inputs=[{"result_id": str(result.pk)}],
        )

    def test_approved_transcript_only_follows_up_once(self):
        candidate = self.attempt("gateway", "complete", text="An external candidate.")
        with patch(
            "core.analysis.reprocessing.follow_up_reprocessing",
            side_effect=self.retain_follow_up,
        ) as follow_up:
            select_transcript(self.case.pk, self.batch.pk, "recording")
            select_transcript(self.case.pk, self.batch.pk, "recording")
            follow_up.assert_called_once_with(candidate, candidate.result)

    def test_cancellation_selects_usable_sibling_without_a_gap_in_processing(self):
        external = self.attempt("gateway", "partial", text="Usable words.")
        private = self.attempt("local", "running")
        with patch("core.analysis.reprocessing.follow_up_reprocessing") as follow_up:
            cancel_run(self.users["owner"], self.case.pk, private.pk)
            follow_up.assert_called_once_with(external, external.result)

    def test_historical_local_result_cannot_start_gateway_followup(self):
        self.attempt("local", "complete", text="Private reading.")
        self.attempt("gateway", "running")
        with patch("core.analysis.reprocessing.follow_up_reprocessing") as follow_up:
            select_transcript(self.case.pk, self.batch.pk, "recording")
            follow_up.assert_not_called()

    def test_blank_or_failed_attempts_do_not_queue_text_analysis(self):
        self.attempt("local", "partial", text="   ")
        self.attempt("gateway", "failed")
        with patch("core.analysis.reprocessing.follow_up_reprocessing") as follow_up:
            select_transcript(self.case.pk, self.batch.pk, "recording")
            follow_up.assert_not_called()

    def test_follow_up_is_bound_to_the_originating_case(self):
        self.attempt("local", "complete", text="Case-specific transcript.")
        stopped = self.attempt("gateway", "failed")
        stopped.case_id = self.other_case.pk
        with patch("core.analysis.reprocessing.follow_up_reprocessing") as follow_up:
            self.assertTrue(follow_up_transcription(stopped))
            follow_up.assert_not_called()

    @override_settings(PANDECT_AI_GATEWAY=TRANSCRIPTION_CONFIG)
    def test_retained_paired_transcription_batch_uses_one_exact_result(
        self,
    ):
        grant = external_grant_kwargs(self.case, "transcription")
        audio = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Synthetic recording",
            file=ContentFile(wav_bytes(), name="recording.wav"),
        )
        batch, _ = start_added_evidence_analysis(
            self.users["editor"], self.case.pk, "audio", audio.pk
        )
        # Retained paired batches pinned a single revision for their external routes.
        batch.scope.pop("external_grants")
        batch.scope["external_grant_revision"] = grant["external_grant_revision"]
        batch.scope["executions"]["transcription:gateway"] = grant["approved_execution"]
        batch.save(update_fields=("scope",))
        for _ in range(4):
            if prepare_case_analysis_batch(batch.pk) == "complete":
                break
        else:
            self.fail("Recording analysis did not finish preparation.")
        runs = batch.runs.filter(operation="transcription")
        self.assertFalse(runs.filter(external_processing=False).exists())
        external = runs.get(external_processing=True)
        self.assertEqual(
            external.parameters["_external_approval"]["revision"],
            grant["external_grant_revision"],
        )
        with patch(
            "core.analysis.gateway.GatewayClient.request",
            return_value={
                "text": "Candidate words.",
                "segments": [{"text": "Candidate words.", "start": 0, "end": 1}],
            },
        ):
            self.assertEqual(process_run(external.pk), str(external.result.pk))
        follow_ups = list(batch.runs.filter(previous_run=external))
        self.assertTrue(follow_ups)
        self.assertEqual(
            {x.inputs[0].get("result_id") for x in follow_ups},
            {str(external.result.pk)},
        )

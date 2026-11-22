from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from audio.models import AudioEvidence
from core.analysis.automatic import start_added_evidence_analysis
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.services import cancel_run, process_run
from core.analysis.testing import external_grant_kwargs, wav_bytes
from core.analysis.tests.test_execution import LOCAL
from core.analysis.tests.test_gateway import TRANSCRIPTION_CONFIG
from core.analysis.transcripts import select_transcript, transcription_profile
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin


@override_settings(PANDECT_PREFER_EXTERNAL_TRANSCRIPTION_PROFILE="")
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

    def test_prefers_private_when_external_finishes_first_and_only_follows_up_once(
        self,
    ):
        self.attempt("gateway", "complete", text="An external candidate.")
        private = self.attempt("local", "running")
        with patch(
            "core.analysis.reprocessing.follow_up_reprocessing",
            side_effect=self.retain_follow_up,
        ) as follow_up:
            select_transcript(self.batch.pk, "recording")
            follow_up.assert_not_called()
            AnalysisRun.objects.filter(pk=private.pk).update(status="partial")
            result = AnalysisResult.objects.create(
                case=self.case,
                run=private,
                data={"items": [{"text": "Private reading."}]},
            )
            select_transcript(self.batch.pk, "recording")
            select_transcript(self.batch.pk, "recording")
            follow_up.assert_called_once_with(private, result)

    def test_waits_for_external_and_honors_only_exact_qualified_profile(self):
        private = self.attempt("local", "complete", text="Private reading.")
        external = self.attempt("gateway", "running")
        profile = transcription_profile(
            private.parameters["_execution"], external.parameters["_execution"]
        )
        with (
            self.settings(PANDECT_PREFER_EXTERNAL_TRANSCRIPTION_PROFILE=profile),
            patch("core.analysis.reprocessing.follow_up_reprocessing") as follow_up,
        ):
            select_transcript(self.batch.pk, "recording")
            follow_up.assert_not_called()
            AnalysisRun.objects.filter(pk=external.pk).update(status="complete")
            result = AnalysisResult.objects.create(
                case=self.case,
                run=external,
                data={"items": [{"text": "External reading."}]},
            )
            select_transcript(self.batch.pk, "recording")
            follow_up.assert_called_once_with(external, result)
            follow_up.reset_mock()
            other = self.attempt(
                "local", "complete", group="other", text="Second reading."
            )
            self.attempt(
                "gateway", "complete", group="other", model="changed", text="External."
            )
            select_transcript(self.batch.pk, "other")
            follow_up.assert_called_once_with(other, other.result)

    def test_cancellation_selects_usable_sibling_without_a_gap_in_processing(self):
        external = self.attempt("gateway", "partial", text="Usable words.")
        private = self.attempt("local", "running")
        with patch("core.analysis.reprocessing.follow_up_reprocessing") as follow_up:
            cancel_run(self.users["owner"], self.case.pk, private.pk)
            follow_up.assert_called_once_with(external, external.result)

    def test_private_text_does_not_wait_for_an_unqualified_external_route(self):
        private = self.attempt("local", "complete", text="Private reading.")
        self.attempt("gateway", "running")
        with patch("core.analysis.reprocessing.follow_up_reprocessing") as follow_up:
            select_transcript(self.batch.pk, "recording")
            follow_up.assert_called_once_with(private, private.result)

    def test_blank_or_failed_attempts_do_not_queue_text_analysis(self):
        self.attempt("local", "partial", text="   ")
        self.attempt("gateway", "failed")
        with patch("core.analysis.reprocessing.follow_up_reprocessing") as follow_up:
            select_transcript(self.batch.pk, "recording")
            follow_up.assert_not_called()

    @override_settings(PANDECT_LOCAL_AI=LOCAL, PANDECT_AI_GATEWAY=TRANSCRIPTION_CONFIG)
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
        batch.scope["executions"]["transcription:gateway"] = grant["approved_execution"]
        batch.save(update_fields=("scope",))
        for _ in range(4):
            if prepare_case_analysis_batch(batch.pk) == "complete":
                break
        else:
            self.fail("Recording analysis did not finish preparation.")
        runs = batch.runs.filter(operation="transcription")
        private = runs.get(external_processing=False)
        external = runs.get(external_processing=True)
        self.assertEqual(
            private.parameters["_transcript_group"],
            external.parameters["_transcript_group"],
        )

        for run, source, text in (
            (external, "gateway.GatewayClient", "External candidate words."),
            (private, "local.LocalClient", "Private candidate words."),
        ):
            with patch(
                f"core.analysis.{source}.request",
                return_value={
                    "text": text,
                    "segments": [{"text": text, "start": 0, "end": 1}],
                },
            ):
                self.assertEqual(process_run(run.pk), str(run.result.pk))
        follow_ups = list(batch.runs.filter(previous_run=private))
        self.assertTrue(follow_ups)
        self.assertFalse(batch.runs.filter(previous_run=external).exists())
        self.assertEqual(sum(x.operation == "entities" for x in follow_ups), 1)
        self.assertEqual(
            {x.inputs[0].get("result_id") for x in follow_ups}, {str(private.result.pk)}
        )

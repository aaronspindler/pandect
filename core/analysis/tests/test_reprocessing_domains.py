"""Bulk preparation covers selected domains, additions, and configured locality."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase
from django.utils import timezone

from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.reprocessing import reprocessing_executions, start_reprocessing
from core.analysis.services import process_run
from core.analysis.testing import image_bytes, wav_bytes
from core.evidence.types import CALL_KINDS, COMMUNICATION_KINDS, KINDS, LOCATION_KINDS
from core.testing import EvidenceViewTestMixin
from phone.models import CallEvidenceAttachment


class ReprocessingDomainTests(EvidenceViewTestMixin, TestCase):
    def source(self, kind, **overrides):
        values = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "title": f"Synthetic {kind}",
        }
        if kind in {"image", "audio", "document"}:
            name, content = {
                "image": ("source.png", image_bytes()),
                "audio": ("source.wav", wav_bytes()),
                "document": ("source.txt", b"Synthetic contact@example.test"),
            }[kind]
            values["file"] = ContentFile(content, name=name)
        if kind in COMMUNICATION_KINDS:
            values |= {
                "sender": self.person,
                "recipient": self.person,
                "occurred_at": timezone.now(),
            }
        if kind in {"text", "text-message"}:
            values["body"] = "The proposed meeting remains unconfirmed."
        if kind in LOCATION_KINDS:
            values |= {
                "address": "Synthetic harbour",
                "latitude": "43.650000",
                "longitude": "-79.380000",
                "observed_at": timezone.now(),
            }
        if kind == "person-location":
            values["person"] = self.person
        if kind == "vehicle-location":
            values["vehicle"] = self.vehicle
        return KINDS[kind].objects.create(**(values | overrides))

    def start(self, kinds, mode="local"):
        batch, _ = start_reprocessing(
            self.users["owner"],
            self.case.pk,
            submission_id=uuid4(),
            kinds=list(kinds),
            mode=mode,
            executions=reprocessing_executions(),
            external_processing=mode != "local",
        )
        return batch

    def drain(self, batch):
        for _ in range(100):
            prepare_case_analysis_batch(batch.pk)
            batch.refresh_from_db()
            if batch.status != "queued":
                self.assertEqual(batch.status, "complete", batch.error_code)
                return
        self.fail("Reprocessing did not finish preparing its selected domains")

    def test_all_domains_include_files_messages_and_every_call_recording(self):
        sources = {kind: self.source(kind) for kind in KINDS}
        attachments = {}
        for kind in CALL_KINDS:
            parent = "phone_call" if kind == "phone-call" else "facetime"
            attachments[kind] = CallEvidenceAttachment.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                transcript="Synthetic supplied transcript.",
                file=ContentFile(wav_bytes(), name="call.wav"),
                **{parent: sources[kind]},
            )
        batch = self.start(KINDS, mode="both")
        self.drain(batch)
        runs = list(batch.runs.all())
        self.assertEqual(
            {source["kind"] for run in runs for source in run.inputs}, set(KINDS)
        )
        self.assertEqual(batch.processed_sources, len(KINDS) + len(attachments))
        self.assertEqual(batch.unavailable_sources, 0)
        for kind, expected in (
            ("image", {"image_metadata", "ocr", "plate_ocr"}),
            ("audio", {"audio_metadata", "transcription"}),
            ("document", {"native_text"}),
            ("text-message", {"sessions", "comm_patterns", "session_summaries"}),
        ):
            with self.subTest(kind=kind):
                self.assertTrue(
                    expected
                    <= {
                        run.operation
                        for run in runs
                        if any(source["kind"] == kind for source in run.inputs)
                    }
                )
        for kind, addition in attachments.items():
            with self.subTest(kind=kind):
                transcriptions = [
                    run
                    for run in runs
                    if run.operation == "transcription"
                    and run.inputs[0]["attachment_id"] == str(addition.pk)
                ]
                self.assertEqual(
                    {run.parameters["backend"] for run in transcriptions},
                    {"local", "gateway"},
                )
                self.assertTrue(
                    any(
                        run.operation == "entities"
                        and any(
                            source["attachment_id"] == str(addition.pk)
                            for source in run.inputs
                        )
                        for run in runs
                    )
                )
                self.assertTrue(
                    any(
                        run.operation == "conversation_link"
                        and any(
                            source["attachment_id"] == str(addition.pk)
                            for source in run.inputs
                        )
                        for run in runs
                    )
                )

    def test_new_call_transcript_receives_exact_communication_followups(self):
        call = self.source("phone-call")
        addition = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            file=ContentFile(wav_bytes(), name="call.wav"),
        )
        batch = self.start(("phone-call",), mode="both")
        self.drain(batch)
        transcription = batch.runs.get(
            operation="transcription", external_processing=True
        )
        words = "Synthetic spoken claim."
        with patch(
            "core.analysis.gateway.GatewayClient.request",
            return_value={
                "text": words,
                "segments": [{"start": 0, "end": 1, "text": words}],
            },
        ):
            process_run(transcription.pk)
        transcription.refresh_from_db()
        self.assertEqual(transcription.status, "complete", transcription.error_code)
        followups = list(
            batch.runs.filter(previous_run=transcription, operation="conversation_link")
        )
        self.assertEqual(len(followups), 3)
        for run in followups:
            self.assertEqual(run.inputs[0]["attachment_id"], str(addition.pk))
            self.assertEqual(run.inputs[0]["result_id"], str(transcription.result.pk))

    def test_message_pages_cover_null_times_and_only_selected_call_kinds(self):
        moment = timezone.now()
        messages = [
            self.source(
                "text-message",
                occurred_at=moment + timedelta(seconds=index) if index < 25 else None,
            )
            for index in range(27)
        ]
        call = self.source("phone-call", occurred_at=moment)
        self.source("facetime", occurred_at=moment)
        batch = self.start(("text-message", "phone-call"))
        self.drain(batch)
        runs = list(batch.runs.all())
        self.assertEqual(
            {
                source["evidence_id"]
                for run in runs
                if run.operation == "sessions"
                for source in run.inputs
            },
            {str(message.pk) for message in messages},
        )
        self.assertEqual(
            {source["kind"] for run in runs for source in run.inputs},
            {"text-message", "phone-call"},
        )
        self.assertTrue(
            any(
                run.operation == "conversation_link"
                and any(source["kind"] == "text-message" for source in run.inputs)
                and any(source["evidence_id"] == str(call.pk) for source in run.inputs)
                for run in runs
            )
        )
        self.assertEqual(batch.processed_sources, 28)

    def test_all_call_transcript_additions_are_prepared_across_page_boundaries(self):
        call = self.source("phone-call")
        additions = [
            CallEvidenceAttachment.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                phone_call=call,
                transcript=f"Synthetic addition {index}.",
            )
            for index in range(26)
        ]
        unselected = self.source("facetime")
        CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            facetime=unselected,
            transcript="Unselected addition.",
        )
        batch = self.start(("phone-call",))
        self.drain(batch)
        self.assertEqual(batch.processed_sources, 27)
        self.assertEqual(
            {
                source["attachment_id"]
                for run in batch.runs.filter(operation="entities")
                for source in run.inputs
            },
            {str(addition.pk) for addition in additions},
        )
        self.assertTrue(
            all(
                source["kind"] == "phone-call"
                for run in batch.runs.all()
                for source in run.inputs
            )
        )

    def test_geographic_methods_follow_each_operator_configured_locality(self):
        self.source("location")
        for geocoder_local, context_local in ((True, False), (False, True)):
            with self.settings(
                PANDECT_GEOCODER_LOCAL=geocoder_local,
                PANDECT_MAP_CONTEXT_LOCAL=context_local,
            ):
                for mode in ("local", "external", "both"):
                    with self.subTest(geocoder_local=geocoder_local, mode=mode):
                        batch = self.start(("location",), mode=mode)
                        self.drain(batch)
                        for operation, local in (
                            ("geocode_locations", geocoder_local),
                            ("geographic_context", context_local),
                        ):
                            runs = list(batch.runs.filter(operation=operation))
                            expected = mode == "both" or local == (mode == "local")
                            self.assertEqual(bool(runs), expected)
                            for run in runs:
                                self.assertEqual(run.external_processing, not local)
                                self.assertEqual(
                                    run.parameters["_execution"]["location"],
                                    "local" if local else "external",
                                )

    def test_failure_total_remains_exact_when_error_details_are_capped(self):
        for _ in range(60):
            self.source("image")
        batch = self.start(("image",))
        with patch(
            "core.analysis.reprocessing.start_run",
            side_effect=ValidationError("Synthetic method failure"),
        ):
            self.drain(batch)
        self.assertEqual(batch.processed_sources, 60)
        self.assertEqual(batch.unavailable_sources, 180)
        self.assertEqual(len(batch.preparation_errors), 100)
        self.assertFalse(batch.runs.exists())

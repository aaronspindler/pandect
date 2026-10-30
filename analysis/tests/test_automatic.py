"""Local evidence batches, exact extraction follow-up and current authorization."""

from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.http import Http404
from django.test import TestCase, override_settings
from django_tasks_db.models import DBTaskResult

from analysis.automatic import start_evidence_analysis
from analysis.models import AnalysisResult, AnalysisRun, GatewayRequest
from analysis.operations import OPERATIONS, get_operation
from analysis.services import process_run, start_run
from cases.models import CaseMembership
from evidence.models import (
    AudioEvidence,
    CallEvidenceAttachment,
    DocumentEvidence,
    ImageEvidence,
    LocationEvidence,
    PhoneCallEvidence,
    TextMessageEvidence,
)
from evidence.tests.test_views import EvidenceViewTestMixin

from .test_media import image_bytes, wav_bytes


class AutomaticAnalysisTests(EvidenceViewTestMixin, TestCase):
    def start(self, item, kind="text", role="owner", submission_id=None):
        return start_evidence_analysis(
            self.users[role],
            self.case.pk,
            kind,
            item.pk,
            submission_id=submission_id or uuid4(),
        )

    def document(self, content=b"Contact source@example.test about the meeting."):
        return DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic document",
            file=ContentFile(content, name="source.txt"),
        )

    def call(self):
        return PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic call",
            sender=self.person,
            recipient=self.person,
        )

    def complete(self, runs):
        for run in runs:
            process_run(str(run.pk))
            run.refresh_from_db()
            self.assertNotIn(
                run.status, {"queued", "running", "failed"}, run.error_code
            )

    def test_text_batch_queues_all_applicable_local_operations_without_network(self):
        item = self.text()
        with (
            patch("analysis.gateway.GatewayClient.request") as gateway,
            patch("analysis.geocoding.Geocoder.lookup") as geocoder,
            patch("analysis.geographic_context.ContextClient.lookup") as maps,
        ):
            runs, created = self.start(item)
            self.assertTrue(created)
            self.assertEqual(
                {run.operation for run in runs},
                {"entities", "style_profiles", "text_topics"},
            )
            self.complete(runs)
            gateway.assert_not_called()
            geocoder.assert_not_called()
            maps.assert_not_called()
        topics = next(run for run in runs if run.operation == "text_topics")
        self.assertIs(topics.parameters["include_summary"], False)
        self.assertTrue(all(not run.external_processing for run in runs))
        self.assertTrue(all(not get_operation(run.operation).external for run in runs))
        self.assertEqual(GatewayRequest.objects.count(), 0)
        item.refresh_from_db()
        self.assertEqual(item.body, "Original café\r\n  preserved.  ")

    def test_retry_reuses_original_batch_after_completion(self):
        item = self.text()
        submission_id = uuid4()
        first, _ = self.start(item, submission_id=submission_id)
        self.complete(first)
        again, created = self.start(item, submission_id=submission_id)
        self.assertFalse(created)
        self.assertEqual([x.pk for x in first], [x.pk for x in again])
        self.assertEqual(DBTaskResult.objects.count(), len(first))
        with self.assertRaises(ValidationError):
            self.start(self.text(), submission_id=submission_id)
        with self.assertRaises(ValidationError):
            self.start(item, role="editor", submission_id=submission_id)

    def test_roles_cross_case_and_revocation_are_enforced(self):
        item = self.text()
        with self.assertRaises(PermissionDenied):
            self.start(item, role="viewer")
        with self.assertRaises(Http404):
            self.start(item, role="outsider")
        with self.assertRaises(Http404):
            start_evidence_analysis(
                self.users["owner"],
                self.other_case.pk,
                "text",
                item.pk,
                submission_id=uuid4(),
            )
        foreign = type(item).objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Other case's original",
            body="Private other case material",
        )
        with self.assertRaises(Http404):
            self.start(foreign)
        runs, _ = self.start(item, role="editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        for run in runs:
            self.assertEqual(process_run(str(run.pk)), "access_removed")
        self.assertEqual(AnalysisResult.objects.count(), 0)
        with self.assertRaises(Http404):
            self.start(item, role="editor")

    def test_batch_rollback_removes_all_runs_and_queue_records(self):
        item = self.text()

        def enqueue(*args, **kwargs):
            if args[2] == "style_profiles":
                raise RuntimeError("Synthetic queue failure")
            return start_run(*args, **kwargs)

        with (
            patch("analysis.automatic.start_run", side_effect=enqueue),
            self.assertRaisesMessage(RuntimeError, "Synthetic queue failure"),
        ):
            self.start(item)
        self.assertEqual(AnalysisRun.objects.count(), 0)
        self.assertEqual(DBTaskResult.objects.count(), 0)
        item.refresh_from_db()

    def test_native_extraction_chains_exact_retained_text_and_retries_once(self):
        document = self.document()
        submission_id = uuid4()
        runs, _ = self.start(document, "document", submission_id=submission_id)
        self.assertEqual([run.operation for run in runs], ["native_text"])
        extraction = runs[0]
        self.complete(runs)
        followups = list(AnalysisRun.objects.filter(previous_run=extraction))
        self.assertEqual({x.operation for x in followups}, {"entities", "text_topics"})
        for run in followups:
            self.assertEqual(run.inputs[0]["result_id"], str(extraction.result.pk))
            self.assertEqual(run.inputs[0]["sha256"], document.sha256)
        self.complete(followups)
        entities = next(x for x in followups if x.operation == "entities")
        finding = entities.result.data["items"][0]
        self.assertEqual(finding["text"], "source@example.test")
        self.assertEqual(
            finding["anchors"][0]["derived_result_id"], str(extraction.result.pk)
        )
        self.assertIn("extraction_ranges", finding["anchors"][0])
        self.assertEqual(process_run(str(extraction.pk)), "complete")
        again, created = self.start(document, "document", submission_id=submission_id)
        self.assertFalse(created)
        self.assertEqual(len(again), 3)
        self.assertEqual(AnalysisRun.objects.count(), 3)
        self.assertEqual(DBTaskResult.objects.count(), 3)

    def test_unsupported_document_keeps_visible_outcome_without_followup(self):
        runs, _ = self.start(self.document(b"\xff\xfe\x00\x00"), "document")
        self.complete(runs)
        self.assertEqual(runs[0].status, "unsupported")
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_failed_followup_enqueue_does_not_publish_partial_batch(self):
        runs, _ = self.start(self.document(), "document")

        def enqueue(*args, **kwargs):
            if args[2] == "text_topics":
                raise RuntimeError("Synthetic queue failure")
            return start_run(*args, **kwargs)

        with patch("analysis.automatic.start_run", side_effect=enqueue):
            self.assertEqual(process_run(str(runs[0].pk)), "processing_failed")
        self.assertEqual(AnalysisResult.objects.count(), 0)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(DBTaskResult.objects.count(), 1)

    def test_revocation_before_extraction_publication_prevents_followup(self):
        document = self.document()
        runs, _ = self.start(document, "document", role="editor")

        def revoke(*args):
            CaseMembership.objects.filter(
                case=self.case, user=self.users["editor"]
            ).delete()
            return {"summary": "Synthetic extraction", "items": []}

        with patch.dict(
            OPERATIONS,
            native_text=replace(
                OPERATIONS["native_text"], function=Mock(side_effect=revoke)
            ),
        ):
            self.assertEqual(process_run(str(runs[0].pk)), "access_removed")
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(AnalysisResult.objects.count(), 0)

    def test_message_adds_local_conversation_analysis(self):
        message = TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic message",
            sender=self.person,
            recipient=self.person,
            body="Meet at the proposed location.",
        )
        runs, _ = self.start(message, "text-message")
        self.assertEqual(
            {run.operation for run in runs},
            {"entities", "style_profiles", "text_topics", "sessions", "comm_patterns"},
        )
        self.complete(runs)

    def test_call_analyses_use_each_supplied_transcript_and_recording(self):
        call = self.call()
        first = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            transcript="Contact source@example.test about the proposed meeting.",
            file=ContentFile(wav_bytes(), name="recording.wav"),
        )
        second = CallEvidenceAttachment.objects.create(
            case=self.case,
            phone_call=call,
            uploaded_by=self.users["owner"],
            transcript="Contact source@example.test about the proposed meeting.",
        )
        runs, _ = self.start(call, "phone-call")
        self.assertEqual(
            {run.operation for run in runs},
            {"audio_metadata", "entities", "copied_text", "text_topics"},
        )
        for run in runs:
            expected = {str(first.pk)}
            if run.operation != "audio_metadata":
                expected.add(str(second.pk))
            self.assertEqual({x["attachment_id"] for x in run.inputs}, expected)
        self.complete(runs)

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=1)
    def test_call_source_limit_is_explicit_and_enqueues_nothing(self):
        call = self.call()
        for index in range(2):
            CallEvidenceAttachment.objects.create(
                case=self.case,
                phone_call=call,
                uploaded_by=self.users["owner"],
                transcript=f"Retained transcript {index}",
            )
        with self.assertRaisesMessage(
            ValidationError, "up to 1 retained call additions"
        ):
            self.start(call, "phone-call")
        self.assertEqual(AnalysisRun.objects.count(), 0)
        self.assertEqual(DBTaskResult.objects.count(), 0)

    def test_empty_call_has_no_local_text_or_audio_analysis(self):
        self.assertEqual(self.start(self.call(), "phone-call"), ([], False))

    def test_media_queues_local_ocr_and_metadata_without_external_approval(self):
        for kind, model, content, name, operation in (
            ("image", ImageEvidence, image_bytes(), "source.png", "image_metadata"),
            ("audio", AudioEvidence, wav_bytes(), "source.wav", "audio_metadata"),
        ):
            with self.subTest(kind=kind):
                item = model.objects.create(
                    case=self.case,
                    uploaded_by=self.users["owner"],
                    title="Synthetic original",
                    file=ContentFile(content, name=name),
                )
                runs, _ = self.start(item, kind)
                self.assertEqual(
                    {x.operation for x in runs},
                    {operation, "ocr"} if kind == "image" else {operation},
                )
                self.complete(runs)

    def test_image_batch_analyzes_the_fresh_local_extraction_version(self):
        image = ImageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic image",
            file=ContentFile(image_bytes(), name="source.png"),
        )
        runs, _ = self.start(image, "image")
        self.assertEqual({x.operation for x in runs}, {"image_metadata", "ocr"})
        from .test_result_integrations import local_ocr_response

        with patch("analysis.media.run_parser", return_value=local_ocr_response()):
            extraction = next(r for r in runs if r.operation == "ocr")
            self.complete([extraction])
        children = list(AnalysisRun.objects.filter(previous_run=extraction))
        self.assertEqual({r.operation for r in children}, {"entities", "text_topics"})
        for run in children:
            self.assertEqual(run.inputs[0]["result_id"], str(extraction.result.pk))
        with patch("analysis.gateway.GatewayClient.request") as gateway:
            self.complete(children)
            gateway.assert_not_called()

    def test_location_batch_excludes_external_and_investigator_defined_queries(self):
        location = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic observation",
            latitude="43.000000",
            longitude="-79.000000",
        )
        runs, _ = self.start(location, "location")
        self.assertEqual(
            {x.operation for x in runs},
            {
                "normalize_addresses",
                "location_quality",
                "spatial_groups",
                "candidate_visits",
                "candidate_trips",
                "movement_routines",
                "colocation",
                "travel_discrepancies",
            },
        )
        with (
            patch("analysis.geocoding.Geocoder.lookup") as geocoder,
            patch("analysis.geographic_context.ContextClient.lookup") as maps,
        ):
            self.complete(runs)
            geocoder.assert_not_called()
            maps.assert_not_called()

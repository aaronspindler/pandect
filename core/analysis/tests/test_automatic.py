"""Local evidence batches, exact extraction follow-up and current authorization."""

from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.http import Http404
from django.test import TestCase, override_settings
from django_tasks_db.models import DBTaskResult

from audio.models import AudioEvidence
from cases.models import CaseMembership
from core.analysis.automatic import (
    start_added_evidence_analysis,
    start_evidence_analysis,
)
from core.analysis.bulk import prepare_case_analysis_batch, resume_case_analysis
from core.analysis.operations import OPERATIONS
from core.analysis.services import start_run
from core.analysis.source_resolution import resolve_inputs
from core.analysis.testing import external_grant_kwargs, image_bytes, wav_bytes
from core.analysis.tests.test_execution import LOCAL
from core.analysis.tests.test_gateway import CONFIG
from core.analysis.worker import process_run
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch, GatewayRequest
from core.storage import retain_uploaded_originals
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import LocationEvidence
from geo.testing import LOCAL_GEOCODER
from image.models import ImageEvidence
from phone.models import CallEvidenceAttachment, PhoneCallEvidence, TextMessageEvidence
from text.models import TextEvidence


@override_settings(PANDECT_LOCAL_AI={})
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

    def prepare_added(self):
        for batch in CaseAnalysisBatch.objects.filter(
            scope__type="added", status="queued"
        ):
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")

    def test_product_upload_queues_once_and_retry_returns_the_same_evidence(self):
        self.login("editor")
        submission_id = str(uuid4())
        upload = self.url("create")
        payload = self.payload(submission_id=submission_id)

        added = self.client.post(upload, payload, HTTP_ACCEPT="application/json")
        self.assertEqual(added.status_code, 201)
        self.assertEqual(set(added.json()), {"type", "id", "detail_url"})
        repeated = self.client.post(upload, payload, HTTP_ACCEPT="application/json")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(added.json(), repeated.json())
        batch = CaseAnalysisBatch.objects.get(case=self.case)
        self.assertFalse(batch.runs.exists())
        self.assertEqual(DBTaskResult.objects.count(), 1)
        self.prepare_added()
        runs = list(batch.runs.all())
        self.assertEqual(
            {run.operation for run in runs},
            {"entities", "text_topics", "style_profiles"},
        )
        self.assertEqual(len(runs), 3)
        for run in runs:
            self.assertEqual(run.inputs[0]["evidence_id"], added.json()["id"])
            self.assertEqual(run.requested_by, self.users["editor"])
            self.assertEqual(run.status, "queued")
            self.assertFalse(run.external_processing)

    @override_settings(PANDECT_AI_GATEWAY=CONFIG, PANDECT_LOCAL_AI=LOCAL)
    def test_intake_defaults_to_one_private_ai_run_with_external_approval(self):
        external_grant_kwargs(self.case, "content_events")
        self.login("editor")
        response = self.client.post(
            self.url("create"), self.payload(), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 201)
        batch = CaseAnalysisBatch.objects.get()
        self.assertEqual(batch.scope["external_grants"], {})
        with patch("core.analysis.gateway.GatewayClient.request") as gateway:
            self.prepare_added()
        gateway.assert_not_called()
        self.assertEqual(batch.runs.filter(operation="entities").count(), 1)
        run = batch.runs.get(operation="content_events")
        self.assertEqual(run.parameters["_execution"]["backend"], "local")
        self.assertFalse(batch.runs.filter(external_processing=True).exists())

    @override_settings(**LOCAL_GEOCODER)
    def test_existing_private_forward_lookup_is_reused_by_intake(self):
        observation = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Test observation",
            address="Synthetic harbour",
        )
        prior, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "geocode_locations",
            [{"kind": "location", "id": str(observation.pk)}],
        )
        batch, _ = start_added_evidence_analysis(
            self.users["owner"],
            self.case.pk,
            "location",
            observation.pk,
        )
        self.prepare_added()
        self.assertTrue(batch.runs.filter(operation="location_quality").exists())
        self.assertEqual(
            list(AnalysisRun.objects.filter(operation="geocode_locations")), [prior]
        )

    def test_duplicate_review_enqueues_only_the_confirmed_new_copy(self):
        earlier = self.document(b"Synthetic file")
        self.login("owner")
        upload = self.url("create")
        submission_id = str(uuid4())

        def submit(**values):
            return self.client.post(
                upload,
                self.payload(
                    "document",
                    submission_id=submission_id,
                    file=SimpleUploadedFile("source.txt", b"Synthetic file"),
                    **values,
                ),
                HTTP_ACCEPT="application/json",
            )

        paused = submit()
        self.assertEqual(paused.status_code, 409)
        self.assertEqual(paused.json()["code"], "duplicate_evidence")
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())

        added = submit(duplicate_confirmation=paused.json()["confirmation"])
        self.assertEqual(added.status_code, 201)
        self.prepare_added()
        run = AnalysisRun.objects.get()
        self.assertEqual(run.inputs[0]["evidence_id"], added.json()["id"])
        self.assertNotEqual(run.inputs[0]["evidence_id"], str(earlier.pk))
        self.assertEqual(run.inputs[0]["sha256"], earlier.sha256)
        self.assertEqual(DBTaskResult.objects.count(), 2)

    def test_failed_auto_queue_rolls_back_evidence_runs_tasks_and_new_file(self):
        self.login("owner")
        earlier = self.document()
        submission_id = str(uuid4())

        def upload():
            return self.client.post(
                self.url("create"),
                self.payload(
                    "image",
                    file=SimpleUploadedFile("source.png", image_bytes()),
                    submission_id=submission_id,
                ),
                HTTP_ACCEPT="application/json",
            )

        existing_files = {path for path in self.media_root.rglob("*") if path.is_file()}
        with patch(
            "core.analysis.tasks.prepare_case_analysis",
            **{
                "enqueue.side_effect": OperationalError("Private queue failure details")
            },
        ):
            failed = upload()
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed.json()["code"], "save_unconfirmed")
        self.assertNotIn("Private queue failure details", failed.content.decode())
        self.assertFalse(ImageEvidence.objects.exists())
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())
        self.assertEqual(
            {path for path in self.media_root.rglob("*") if path.is_file()},
            existing_files,
        )
        self.assertTrue(earlier.file.storage.exists(earlier.file.name))
        self.assertEqual(upload().status_code, 201)
        self.prepare_added()
        self.assertEqual(AnalysisRun.objects.count(), 3)

    def test_lost_commit_confirmation_keeps_originals_and_resolves_on_retry(self):
        self.login("owner")
        call = self.call()
        document_submission = str(uuid4())
        attachment_submission = str(uuid4())

        @contextmanager
        def lose_confirmation():
            with retain_uploaded_originals() as originals:
                yield originals
            raise OperationalError("Synthetic lost commit confirmation")

        def upload_document():
            return self.client.post(
                self.url("create"),
                self.payload(
                    "document",
                    submission_id=document_submission,
                    file=SimpleUploadedFile("source.txt", b"Synthetic retained text"),
                ),
            )

        def upload_attachment():
            return self.client.post(
                self.url("add_attachment", call, "phone-call"),
                {
                    "submission_id": attachment_submission,
                    "file": SimpleUploadedFile("call.wav", wav_bytes()),
                },
            )

        with (
            patch(
                "core.evidence.retention.retain_uploaded_originals", lose_confirmation
            ),
            patch(
                "phone.call_attachments.retain_uploaded_originals", lose_confirmation
            ),
        ):
            document_error = upload_document()
            attachment_error = upload_attachment()
        for response in (document_error, attachment_error):
            self.assertContains(response, "retry this submission", status_code=503)
            self.assertNotContains(response, "Evidence added.", status_code=503)
            self.assertNotContains(
                response, "Transcript or audio added.", status_code=503
            )
        document = DocumentEvidence.objects.get(submission_id=document_submission)
        attachment = call.attachments.get(submission_id=attachment_submission)
        for original in (document.file, attachment.file):
            self.assertTrue(original.storage.exists(original.name))
        self.assertEqual(CaseAnalysisBatch.objects.count(), 2)
        self.assertEqual(DBTaskResult.objects.count(), 2)

        self.assertRedirects(
            upload_document(), self.url("detail", document, "document")
        )
        self.assertRedirects(
            upload_attachment(),
            self.url("detail", call, "phone-call") + f"#attachment-{attachment.pk}",
        )
        self.assertEqual(DocumentEvidence.objects.count(), 1)
        self.assertEqual(call.attachments.count(), 1)
        self.assertEqual(CaseAnalysisBatch.objects.count(), 2)
        self.assertEqual(DBTaskResult.objects.count(), 2)

    def test_initial_call_queues_metadata_and_exact_supplied_content(self):
        self.login("owner")
        upload = self.url("create")
        empty_call = self.client.post(
            upload, self.payload("phone-call"), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(empty_call.status_code, 201)
        self.prepare_added()
        check = AnalysisRun.objects.get()
        self.assertEqual(check.operation, "conversation_link")
        self.assertIsNone(check.inputs[0]["attachment_id"])

        added = self.client.post(
            upload,
            self.payload("phone-call", transcript="Contact source@example.test."),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(added.status_code, 201)
        call = PhoneCallEvidence.objects.get(pk=added.json()["id"])
        attachment = call.attachments.get()
        self.prepare_added()
        batch = CaseAnalysisBatch.objects.get(scope__id=str(call.pk))
        text_check = batch.runs.get(operation="entities")
        self.assertEqual(text_check.inputs[0]["attachment_id"], str(attachment.pk))
        self.assertEqual(batch.runs.filter(operation="conversation_link").count(), 2)

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=1)
    def test_appended_call_only_queues_for_new_attachment_and_reuses_it_on_retry(self):
        self.login("editor")
        call = self.call()
        CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=call,
            transcript="Earlier retained transcript",
        )
        submission_id = str(uuid4())

        def submit():
            return self.client.post(
                self.url("add_attachment", call, "phone-call"),
                {
                    "submission_id": submission_id,
                    "transcript": "New retained transcript",
                    "file": SimpleUploadedFile("call.wav", wav_bytes()),
                },
                HTTP_ACCEPT="application/json",
            )

        added = submit()
        self.assertEqual(added.status_code, 201)
        repeated = submit()
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(added.json(), repeated.json())
        self.assertEqual(CaseAnalysisBatch.objects.count(), 1)
        self.prepare_added()
        runs = list(AnalysisRun.objects.all())
        self.assertEqual(
            {run.operation for run in runs},
            {"audio_metadata", "entities", "text_topics", "conversation_link"},
        )
        self.assertEqual(len(runs), 4)
        for run in runs:
            self.assertEqual(len(run.inputs), 1)
            self.assertEqual(run.inputs[0]["attachment_id"], added.json()["id"])

    def test_failed_preparation_preserves_evidence_and_resumes_once(self):
        self.login("owner")
        added = self.client.post(
            self.url("create"),
            self.payload(),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(added.status_code, 201)
        batch = CaseAnalysisBatch.objects.get()
        with patch(
            "core.analysis.reprocessing.start_run",
            side_effect=OperationalError("Synthetic interrupted preparation"),
        ):
            self.assertEqual(
                prepare_case_analysis_batch(batch.pk), "preparation_failed"
            )
        self.assertTrue(
            TextEvidence.objects.filter(case=self.case, pk=added.json()["id"]).exists()
        )
        self.assertFalse(batch.runs.exists())
        self.assertTrue(
            resume_case_analysis(self.users["owner"], self.case.pk, batch.pk)[1]
        )
        self.assertFalse(
            resume_case_analysis(self.users["owner"], self.case.pk, batch.pk)[1]
        )
        self.prepare_added()
        self.assertEqual(batch.runs.filter(operation="entities").count(), 1)

    def test_text_batch_queues_all_applicable_local_operations_without_network(self):
        item = self.text()
        with (
            patch("core.analysis.gateway.GatewayClient.request") as gateway,
            patch("geo.analysis.external.geocoding.Geocoder.lookup") as geocoder,
            patch(
                "geo.analysis.external.geographic_context.ContextClient.lookup"
            ) as maps,
        ):
            runs, created = self.start(item)
            self.assertTrue(created)
            self.assertEqual(
                {run.operation for run in runs},
                {"entities", "text_topics"},
            )
            self.complete(runs)
            gateway.assert_not_called()
            geocoder.assert_not_called()
            maps.assert_not_called()
        topics = next(run for run in runs if run.operation == "text_topics")
        self.assertIs(topics.parameters["include_summary"], False)
        self.assertTrue(all(not run.external_processing for run in runs))
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
        editor_runs, created = self.start(
            item, role="editor", submission_id=submission_id
        )
        self.assertTrue(created)
        self.assertTrue(
            {run.pk for run in first}.isdisjoint(run.pk for run in editor_runs)
        )
        self.assertTrue(
            all(run.requested_by == self.users["editor"] for run in editor_runs)
        )

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
            if args[2] == "text_topics":
                raise RuntimeError("Synthetic queue failure")
            return start_run(*args, **kwargs)

        with (
            patch("core.analysis.automatic.start_run", side_effect=enqueue),
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
        self.assertCountEqual(
            DBTaskResult.objects.values_list("task_path", flat=True),
            ["core.analysis.tasks.process_analysis"] * 3
            + ["core.case_search.parent_index.index_children"],
        )

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

        with patch("core.analysis.automatic.start_run", side_effect=enqueue):
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

    def test_single_message_checks_content_without_comparing_conversations(self):
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
            {"entities", "text_topics"},
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
            {"audio_metadata", "entities", "text_topics"},
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
                    {operation, "ocr", "plate_ocr"} if kind == "image" else {operation},
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
        self.assertEqual(
            {x.operation for x in runs}, {"image_metadata", "ocr", "plate_ocr"}
        )
        from core.analysis.testing import local_ocr_response

        with patch("image.analysis.ai.run_parser", return_value=local_ocr_response()):
            extraction = next(r for r in runs if r.operation == "ocr")
            self.complete([extraction])
        children = list(AnalysisRun.objects.filter(previous_run=extraction))
        self.assertEqual({r.operation for r in children}, {"entities", "text_topics"})
        self.assertEqual(len(children), 2)
        self.assertIn(
            extraction.inputs[0]["id"], extraction.result.data["visible_structure"]
        )
        for run in children:
            self.assertEqual(run.inputs[0]["result_id"], str(extraction.result.pk))
            (source,) = resolve_inputs(run, self.case)
            self.assertEqual(source.text, "Synthetic extraction phrase")
            self.assertEqual(len(source.segments), 1)
        with patch("core.analysis.gateway.GatewayClient.request") as gateway:
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
        self.assertEqual({x.operation for x in runs}, {"location_quality"})
        address = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic address",
            address="12 Example Street",
        )
        address_runs, _ = self.start(address, "location")
        self.assertEqual(
            {x.operation for x in address_runs},
            {"location_quality", "normalize_addresses"},
        )
        with (
            patch("geo.analysis.external.geocoding.Geocoder.lookup") as geocoder,
            patch(
                "geo.analysis.external.geographic_context.ContextClient.lookup"
            ) as maps,
        ):
            self.complete(runs + address_runs)
            geocoder.assert_not_called()
            maps.assert_not_called()

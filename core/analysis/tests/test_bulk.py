"""Case-wide local checks keep bounds, attribution and retry-safe progress."""

from unittest.mock import patch
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import OperationalError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from cases.models import CaseMembership
from core.analysis.bulk import prepare_case_analysis_batch, start_case_analysis
from core.analysis.services import process_run
from core.evidence.types import CALL_KINDS, COMMUNICATION_KINDS, FILE_KINDS, KINDS
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from people.models import Person
from phone.models import CallEvidenceAttachment


class CaseAnalysisTests(EvidenceViewTestMixin, TestCase):
    def source(self, kind, **overrides):
        values = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "title": f"Synthetic {kind}",
        }
        if kind in FILE_KINDS:
            values["file"] = ContentFile(
                b"Contact file@example.test", name="source.txt"
            )
        if kind in COMMUNICATION_KINDS:
            values |= {"sender": self.person, "recipient": self.person}
        if kind in {"text", "text-message"}:
            values["body"] = "Contact source@example.test"
        if kind in {"location", "person-location", "vehicle-location"}:
            values["address"] = "Synthetic harbour"
        if kind == "person-location":
            values["person"] = self.person
        if kind == "vehicle-location":
            values["vehicle"] = self.vehicle
        return KINDS[kind].objects.create(**(values | overrides))

    def start(self, role="owner", submission_id=None, case=None):
        return start_case_analysis(
            self.users[role],
            (case or self.case).pk,
            submission_id=submission_id or uuid4(),
        )

    def drain(self, batch):
        for _ in range(100):
            result = prepare_case_analysis_batch(str(batch.pk))
            batch.refresh_from_db()
            if batch.status != "queued":
                self.assertEqual(result, "complete")
                return
        self.fail("Case analysis did not finish preparing checks")

    def url(self, name="rerun_all"):
        return reverse(f"core:analysis:{name}", args=[self.case.pk])

    def test_all_types_use_local_checks_and_preserve_existing_results(self):
        sources = {kind: self.source(kind) for kind in KINDS}
        for kind in CALL_KINDS:
            parent = "phone_call" if kind == "phone-call" else "facetime"
            CallEvidenceAttachment.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                transcript="Contact transcript@example.test",
                **{parent: sources[kind]},
            )
        batch, created = self.start(role="editor")
        self.assertTrue(created)
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertEqual(DBTaskResult.objects.count(), 1)
        self.drain(batch)
        runs = AnalysisRun.objects.filter(case=self.case)
        self.assertEqual(batch.processed_sources, len(KINDS))
        self.assertEqual(batch.unavailable_sources, 0)
        self.assertEqual(batch.queued_runs, runs.count())
        self.assertEqual(
            {entry["kind"] for run in runs for entry in run.inputs}, set(KINDS)
        )
        self.assertFalse(runs.exclude(requested_by=self.users["editor"]).exists())
        self.assertFalse(runs.filter(external_processing=True).exists())
        self.assertEqual(
            set(runs.values_list("operation", flat=True)),
            {
                "entities",
                "text_topics",
                "native_text",
                "image_metadata",
                "ocr",
                "plate_ocr",
                "audio_metadata",
                "location_quality",
                "normalize_addresses",
            },
        )
        for run in runs.filter(operation="text_topics"):
            self.assertIs(run.parameters["include_summary"], False)
        original_run = runs.filter(operation="entities").first()
        process_run(str(original_run.pk))
        original_run.refresh_from_db()
        self.assertEqual(original_run.status, "complete")
        original_result = AnalysisResult.objects.get(run=original_run)
        retained_data = original_result.data
        later, _ = self.start()
        self.drain(later)
        original_result.refresh_from_db()
        self.assertEqual(original_result.data, retained_data)
        self.assertEqual(
            AnalysisRun.objects.count(), batch.queued_runs + later.queued_runs
        )

    def test_archived_foreign_inconsistent_and_new_sources_are_excluded(self):
        active = self.source("text")
        self.source(
            "text",
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        self.source("text", case=self.other_case)
        foreign_person = Person.objects.create(case=self.other_case, first_name="Other")
        inconsistent = self.source("text-message")
        KINDS["text-message"].objects.filter(pk=inconsistent.pk).update(
            sender=foreign_person
        )
        batch, _ = self.start()
        self.source("text")
        self.drain(batch)
        self.assertEqual(batch.processed_sources, 1)
        self.assertEqual(batch.queued_runs, 2)
        for run in AnalysisRun.objects.all():
            self.assertEqual(run.inputs[0]["evidence_id"], str(active.pk))

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=1)
    def test_pages_cover_every_source_and_more_than_fifty_call_additions(self):
        texts = [self.source("text") for _ in range(27)]
        call = self.source("phone-call")
        attachments = [
            CallEvidenceAttachment.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                phone_call=call,
                transcript=f"Retained addition {index}",
            )
            for index in range(52)
        ]
        batch, _ = self.start()
        prepare_case_analysis_batch(str(batch.pk))
        batch.refresh_from_db()
        self.assertEqual(batch.processed_sources, 25)
        self.assertEqual(batch.status, "queued")
        self.drain(batch)
        self.assertEqual(batch.processed_sources, len(texts) + len(attachments))
        self.assertEqual(batch.queued_runs, 2 * batch.processed_sources)
        selected_additions = {
            entry["attachment_id"]
            for run in AnalysisRun.objects.all()
            for entry in run.inputs
            if entry["attachment_id"]
        }
        self.assertEqual(selected_additions, {str(item.pk) for item in attachments})

    def test_submission_retry_recovers_same_batch_and_rejects_other_actor_or_case(self):
        self.source("text")
        batch, _ = self.start()
        self.drain(batch)
        queued = DBTaskResult.objects.count()
        again, created = self.start(submission_id=batch.pk)
        self.assertFalse(created)
        self.assertEqual(again.pk, batch.pk)
        self.assertEqual(prepare_case_analysis_batch(str(batch.pk)), "complete")
        self.assertEqual(DBTaskResult.objects.count(), queued)
        with self.assertRaises(ValidationError):
            self.start(role="editor", submission_id=batch.pk)
        with self.assertRaises(ValidationError):
            self.start(role="outsider", case=self.other_case, submission_id=batch.pk)

    def test_unavailable_original_does_not_block_other_evidence(self):
        missing = self.source("document")
        missing.file.storage.delete(missing.file.name)
        self.source("text")
        batch, _ = self.start()
        self.drain(batch)
        self.assertEqual(batch.unavailable_sources, 1)
        self.assertEqual(batch.processed_sources, 2)
        self.assertEqual(batch.queued_runs, 2)

    def test_failed_initial_enqueue_rolls_back_request(self):
        with (
            patch(
                "core.analysis.tasks.prepare_case_analysis",
                **{"enqueue.side_effect": OperationalError},
            ),
            self.assertRaises(OperationalError),
        ):
            self.start()
        self.assertFalse(CaseAnalysisBatch.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())

    def test_failed_continuation_rolls_back_page_and_allows_fresh_rerun(self):
        self.source("text")
        batch, _ = self.start()
        with patch(
            "core.analysis.tasks.prepare_case_analysis",
            **{"enqueue.side_effect": OperationalError},
        ):
            result = prepare_case_analysis_batch(str(batch.pk))
        self.assertEqual(result, "preparation_failed")
        batch.refresh_from_db()
        self.assertEqual(batch.status, "failed")
        self.assertEqual(batch.processed_sources, 0)
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertEqual(DBTaskResult.objects.count(), 1)
        fresh, _ = self.start()
        self.drain(fresh)
        self.assertEqual(fresh.queued_runs, 2)

    def test_access_revocation_between_pages_stops_preparation_and_workers(self):
        for _ in range(26):
            self.source("text")
        batch, _ = self.start(role="editor")
        prepare_case_analysis_batch(str(batch.pk))
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(prepare_case_analysis_batch(str(batch.pk)), "access_removed")
        batch.refresh_from_db()
        self.assertEqual(batch.status, "failed")
        self.assertEqual(batch.processed_sources, 25)
        run = AnalysisRun.objects.first()
        self.assertEqual(process_run(str(run.pk)), "access_removed")
        self.assertFalse(AnalysisResult.objects.exists())

    def test_unverified_requester_cannot_prepare_checks(self):
        self.source("text")
        batch, _ = self.start()
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        self.assertEqual(prepare_case_analysis_batch(str(batch.pk)), "access_removed")
        self.assertFalse(AnalysisRun.objects.exists())

    def test_button_roles_post_csrf_validation_and_duplicate_click(self):
        self.source("text")
        for role, status in (
            ("owner", 302),
            ("editor", 302),
            ("viewer", 403),
            ("outsider", 404),
        ):
            self.login(role)
            index = self.client.get(self.url("processing"))
            if role in {"owner", "editor"}:
                self.assertContains(index, "Rerun all analysis")
                submission_id = str(
                    index.context["case_analysis_form"]["submission_id"].value()
                )
            else:
                self.assertNotIn(b"Rerun all analysis", index.content)
                submission_id = str(uuid4())
            response = self.client.post(self.url(), {"submission_id": submission_id})
            self.assertEqual(response.status_code, status)
            if status == 302:
                self.assertEqual(
                    self.client.post(
                        self.url(), {"submission_id": submission_id}
                    ).status_code,
                    302,
                )
        self.assertEqual(CaseAnalysisBatch.objects.count(), 2)
        self.assertEqual(DBTaskResult.objects.count(), 2)
        self.login("owner")
        self.assertEqual(self.client.get(self.url()).status_code, 405)
        self.client.post(self.url(), {"submission_id": "invalid"})
        self.assertEqual(CaseAnalysisBatch.objects.count(), 2)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.users["owner"])
        self.assertEqual(
            csrf_client.post(self.url(), {"submission_id": str(uuid4())}).status_code,
            403,
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.login("editor")
        self.assertEqual(
            self.client.post(self.url(), {"submission_id": str(uuid4())}).status_code,
            404,
        )

    def test_empty_request_and_safe_queue_error_are_visible(self):
        batch, _ = self.start()
        self.drain(batch)
        self.assertEqual(batch.processed_sources, 0)
        self.login("owner")
        self.assertContains(
            self.client.get(self.url("processing"), {"batch": batch.pk}),
            "No active evidence",
        )
        with patch(
            "core.analysis.tasks.prepare_case_analysis",
            **{"enqueue.side_effect": OperationalError("Private connection details")},
        ):
            response = self.client.post(
                self.url(), {"submission_id": str(uuid4())}, follow=True
            )
        self.assertContains(response, "Analysis could not be queued")
        self.assertNotContains(response, "Private connection details")

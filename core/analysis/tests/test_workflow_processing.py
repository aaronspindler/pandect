"""Preparation, readiness and deliberate import checks remain case scoped."""

from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.bulk import prepare_case_analysis_batch, start_case_analysis
from core.analysis.form_base import selection
from core.analysis.forms import analysis_form
from core.analysis.import_checks import imported_sources
from core.analysis.services import process_run, start_run
from core.models import AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import VehicleLocationEvidence, VehicleLocationImport


class WorkflowProcessingTests(EvidenceViewTestMixin, TestCase):
    def document(self):
        return DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Retained export",
            file=ContentFile(b"Contact synthetic@example.test", name="export.txt"),
        )

    def test_batch_completion_tracks_followups_separately_from_preparation(self):
        self.document()
        batch, _ = start_case_analysis(
            self.users["owner"], self.case.pk, submission_id=uuid4()
        )
        while batch.status == "queued":
            prepare_case_analysis_batch(batch.pk)
            batch.refresh_from_db()
        self.assertEqual(batch.status, "complete")
        self.assertEqual(batch.runs.count(), 1)
        run = batch.runs.get()
        self.assertEqual(run.status, "queued")
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        self.assertEqual(batch.runs.count(), 3)
        self.assertEqual(batch.runs.filter(status="queued").count(), 2)
        self.login("viewer")
        response = self.client.get(
            reverse("core:analysis:processing", args=[self.case.pk]),
            {"batch": batch.pk},
        )
        self.assertContains(response, "Checks queued")
        self.assertEqual(response.context["active_runs"].paginator.count, 2)
        self.assertContains(response, 'id="in-progress-heading"')
        self.assertContains(response, "Extract document text")
        self.login("outsider")
        self.assertEqual(
            self.client.get(
                reverse("core:analysis:processing", args=[self.case.pk])
            ).status_code,
            404,
        )

    def test_preparation_retains_bounded_safe_failure_details(self):
        document = self.document()
        document.file.storage.delete(document.file.name)
        batch, _ = start_case_analysis(
            self.users["owner"], self.case.pk, submission_id=uuid4()
        )
        prepare_case_analysis_batch(batch.pk)
        batch.refresh_from_db()
        self.assertEqual(batch.unavailable_sources, 1)
        self.assertEqual(batch.preparation_errors[0]["evidence_id"], str(document.pk))
        self.assertNotIn(document.file.name, batch.preparation_errors[0]["message"])

    def test_editor_retry_and_extraction_followups_keep_parent_batch(self):
        self.document()
        batch, _ = start_case_analysis(
            self.users["owner"], self.case.pk, submission_id=uuid4()
        )
        prepare_case_analysis_batch(batch.pk)
        prior = batch.runs.get()
        AnalysisRun.objects.filter(pk=prior.pk).update(
            status="failed", error_code="preparation_failed"
        )
        self.login("editor")
        response = self.client.post(
            reverse("core:analysis:retry", args=[self.case.pk, prior.pk]),
            {"submission_id": uuid4()},
        )
        self.assertEqual(response.status_code, 302)
        retried = AnalysisRun.objects.get(previous_run=prior)
        self.assertEqual(retried.batch_id, batch.pk)
        self.assertEqual(retried.requested_by, self.users["editor"])
        process_run(retried.pk)
        retried.refresh_from_db()
        self.assertEqual(retried.status, "complete", retried.error_code)
        followups = AnalysisRun.objects.filter(previous_run=retried)
        self.assertEqual(followups.count(), 2)
        self.assertTrue(all(run.batch_id == batch.pk for run in followups))

    def test_readiness_requires_exact_extraction_without_parsing_on_get(self):
        document = self.document()
        with patch(
            "core.analysis.local.build_opener", side_effect=AssertionError("No request")
        ):
            form = analysis_form(case=self.case, operation="entities")
        original = f"document:{document.pk}"
        self.assertEqual(form.readiness[original]["label"], "Needs extraction")
        bound = analysis_form(
            {
                "operation": "entities",
                "submission_id": str(uuid4()),
                "sources": [original],
            },
            case=self.case,
            operation="entities",
        )
        self.assertFalse(bound.is_valid())
        self.assertIn("No sources were dropped", str(bound.errors))
        run, _ = start_run(
            self.users["owner"], self.case.pk, "native_text", [selection(original)]
        )
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        form = analysis_form(case=self.case, operation="entities")
        extracted = f"document:{document.pk}:-:{run.result.pk}"
        self.assertEqual(form.readiness[extracted]["label"], "Ready")

    def test_import_checks_batch_rows_and_reject_foreign_or_incomplete_imports(self):
        document = self.document()
        imported = VehicleLocationImport.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            document=document,
            timezone="UTC",
            status="complete",
            completed_at=timezone.now(),
        )
        for index in range(28):
            VehicleLocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                vehicle=self.vehicle,
                title=f"Imported {index}",
                address=f"{index} Harbour Road",
                import_batch=imported,
                source_row=index + 2,
                raw_row={"Asset": "Synthetic", "Address": f"{index} Harbour Road"},
            )
        scope = {"type": "vehicle", "id": str(imported.pk)}
        self.assertEqual(imported_sources(self.case, scope)[1].count(), 28)
        batch, _ = start_case_analysis(
            self.users["owner"], self.case.pk, submission_id=uuid4(), scope=scope
        )
        prepare_case_analysis_batch(batch.pk)
        batch.refresh_from_db()
        self.assertEqual(batch.processed_sources, 28)
        self.assertEqual(batch.queued_runs, 2)
        self.assertEqual(AnalysisRun.objects.filter(batch=batch).count(), 2)
        self.assertTrue(all(len(run.inputs) == 28 for run in batch.runs.all()))
        self.assertFalse(batch.runs.filter(external_processing=True).exists())
        again, created = start_case_analysis(
            self.users["owner"], self.case.pk, submission_id=batch.pk, scope=scope
        )
        self.assertEqual(again.pk, batch.pk)
        self.assertFalse(created)
        self.login("viewer")
        self.assertEqual(
            self.client.post(
                reverse(
                    "core:analysis:import_checks",
                    args=[self.case.pk, "vehicle", imported.pk],
                ),
                {"submission_id": str(uuid4())},
            ).status_code,
            403,
        )

    def processing_url(self):
        return reverse("core:analysis:processing", args=[self.case.pk])

    def retained_run(self, source, *, status="complete", kind="document"):
        return AnalysisRun.objects.create(
            case=source.case,
            requested_by=source.case.owner,
            operation="native_text" if kind == "document" else "entities",
            method_version="synthetic-test.v1",
            inputs=[{"kind": kind, "evidence_id": str(source.pk)}],
            status=status,
        )

    def vehicle_import(self, *, status="complete", case=None, title="Retained CSV"):
        case = case or self.case
        document = DocumentEvidence.objects.create(
            case=case,
            uploaded_by=case.owner,
            title=title,
            file=ContentFile(b"Synthetic retained CSV", name="locations.csv"),
        )
        return VehicleLocationImport.objects.create(
            case=case,
            uploaded_by=case.owner,
            document=document,
            timezone="America/Toronto",
            status=status,
        )

    def test_history_filters_and_page_do_not_hide_current_work(self):
        document = self.document()
        complete = [self.retained_run(document) for _ in range(26)]
        self.retained_run(document, status="failed")
        current_source = self.text()
        current = self.retained_run(current_source, status="running", kind="text")
        batch = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.users["owner"]
        )
        imported = self.vehicle_import(status="queued")
        foreign_document = DocumentEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Foreign source must stay private",
            file=ContentFile(b"Private CSV", name="foreign.csv"),
        )
        self.retained_run(foreign_document, status="running")
        self.login("viewer")
        response = self.client.get(
            self.processing_url(), {"kind": "document", "status": "complete", "page": 2}
        )
        self.assertEqual(response.context["page_obj"].paginator.count, 26)
        self.assertEqual(
            [run.pk for run in response.context["page_obj"]], [complete[0].pk]
        )
        self.assertEqual(
            [run.pk for run in response.context["active_runs"]], [current.pk]
        )
        self.assertEqual(
            [row.pk for row in response.context["active_batches"]], [batch.pk]
        )
        self.assertEqual(
            [row.pk for row in response.context["active_imports"]], [imported.pk]
        )
        self.assertContains(
            response,
            reverse("core:analysis:detail", args=[self.case.pk, complete[0].pk])
            + "?kind=document&amp;status=complete&amp;history_page=2",
        )
        self.assertNotContains(response, "Foreign source must stay private")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotContains(response, "Rerun all analysis")
        self.assertNotContains(response, "Choose analysis")

    def test_processing_current_work_and_import_history_are_bounded_independently(self):
        document = self.document()
        for _ in range(12):
            self.retained_run(document, status="queued")
        imports = [self.vehicle_import() for _ in range(26)]
        current_import = self.vehicle_import(status="running", title="CSV in progress")
        self.login("editor")
        response = self.client.get(
            self.processing_url(), {"active_page": 2, "imports_page": 2}
        )
        self.assertEqual(len(response.context["active_runs"]), 2)
        self.assertEqual(response.context["active_runs"].paginator.count, 12)
        self.assertEqual(len(response.context["import_history"]), 2)
        self.assertEqual(response.context["import_history"].paginator.count, 27)
        self.assertEqual(
            {row.pk for row in response.context["import_history"]},
            {imports[0].pk, imports[1].pk},
        )
        self.assertEqual(
            [row.pk for row in response.context["active_imports"]], [current_import.pk]
        )
        self.assertContains(
            response,
            reverse(
                "geo:vehicle_import_review", args=[self.case.pk, current_import.pk]
            ),
        )
        self.assertContains(response, 'id="import-history" open')
        self.assertContains(response, "Rerun all analysis")

    def test_processing_rechecks_access_and_rejects_foreign_batches_and_import_sources(
        self,
    ):
        foreign_import = self.vehicle_import(
            status="running", case=self.other_case, title="Foreign CSV"
        )
        invalid_import = self.vehicle_import(status="running", title="Mismatched CSV")
        DocumentEvidence.objects.filter(pk=invalid_import.document_id).update(
            case=self.other_case
        )
        foreign_batch = CaseAnalysisBatch.objects.create(
            case=self.other_case, requested_by=self.users["outsider"]
        )
        self.login("viewer")
        response = self.client.get(self.processing_url())
        self.assertNotContains(response, foreign_import.document.title)
        self.assertNotContains(response, invalid_import.document.title)
        self.assertContains(response, "Nothing is being processed right now.")
        for value in (foreign_batch.pk, "invalid"):
            self.assertEqual(
                self.client.get(self.processing_url(), {"batch": value}).status_code,
                404,
            )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["viewer"]
        ).delete()
        self.assertEqual(self.client.get(self.processing_url()).status_code, 404)
        self.login("outsider")
        self.assertEqual(self.client.get(self.processing_url()).status_code, 404)

    def test_processing_get_is_read_only_and_validates_history_filters(self):
        source = self.document()
        self.retained_run(source, status="queued")
        self.login("owner")
        before = (AnalysisRun.objects.count(), CaseAnalysisBatch.objects.count())
        response = self.client.get(
            self.processing_url(), {"kind": "untrusted", "status": "invented"}
        )
        self.assertEqual(response.context["history_filters"], {})
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertEqual(
            (AnalysisRun.objects.count(), CaseAnalysisBatch.objects.count()), before
        )
        self.assertContains(response, "Rerun all analysis")

    def test_imports_ready_for_review_remain_visible_with_current_work(self):
        ready = self.vehicle_import(status="review", title="Locations ready for review")
        self.login("editor")
        response = self.client.get(self.processing_url())
        self.assertEqual(
            [item.pk for item in response.context["active_imports"]], [ready.pk]
        )
        self.assertContains(response, "Ready to review")
        self.assertNotContains(response, "Nothing is being processed right now")
        self.assertContains(
            response,
            reverse("geo:vehicle_import_review", args=[self.case.pk, ready.pk]),
        )

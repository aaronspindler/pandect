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
from core.analysis.operations import OPERATIONS
from core.analysis.services import process_run, start_run
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import (
    VehicleLocationEvidence,
    VehicleLocationImport,
    VehicleTripGeneration,
    VehicleTripState,
)
from geo.trip_processing import METHOD_VERSION


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
        self.assertEqual(response.context["active_runs"].paginator.count, 0)
        self.assertEqual(response.context["page_obj"].paginator.count, 3)
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
        self.login("owner")
        url = reverse("core:analysis:create", args=[self.case.pk])
        response = self.client.get(url, {"operation": "entities"})
        self.assertContains(response, "Prepare source")
        self.assertNotContains(response, 'type="checkbox" name="sources"')
        self.assertContains(response, 'type="submit" disabled>Start analysis')
        response = self.client.post(
            url,
            {"operation": "entities", "submission_id": uuid4(), "sources": [original]},
        )
        self.assertContains(response, "No sources were dropped")
        self.assertContains(response, f'name="remove_source" value="{original}"')
        self.assertContains(
            response, f'type="hidden" name="sources" value="{original}"'
        )
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

    def retained_run(self, source, *, kind="document", **overrides):
        values = {
            "case": source.case,
            "requested_by": source.case.owner,
            "operation": "native_text" if kind == "document" else "entities",
            "method_version": "synthetic-test.v1",
            "inputs": [{"kind": kind, "evidence_id": str(source.pk)}],
            "status": "complete",
        }
        return AnalysisRun.objects.create(**(values | overrides))

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
        self.retained_run(foreign_document)
        self.login("viewer")
        response = self.client.get(
            self.processing_url(), {"kind": "document", "status": "complete", "page": 2}
        )
        self.assertEqual(response.context["page_obj"].paginator.count, 26)
        self.assertEqual(
            [run.pk for run in response.context["page_obj"]], [complete[0].pk]
        )
        self.assertEqual(response.context["analysis_run_total"], 26)
        self.assertEqual(
            response.context["analysis_run_counts"],
            [{"label": "Extract document text", "count": 26}],
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
        self.assertNotContains(response, "Run local checks")
        self.assertNotContains(response, "Choose analysis")

    def test_analysis_counts_include_saved_outcomes_across_methods_and_batches(self):
        document = self.document()
        batch = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.users["owner"]
        )
        original = self.retained_run(document, attempts=3)
        rerun = self.retained_run(
            document,
            status="partial",
            method_version="synthetic-test.v2",
            previous_run=original,
            batch=batch,
        )
        for status in ("complete", "empty", "insufficient", "unsupported"):
            self.retained_run(document, operation="entities", status=status)
        self.retained_run(document, operation="ocr")
        self.retained_run(document, operation="ocr", status="empty")
        for status in ("queued", "running", "failed", "cancelled"):
            self.retained_run(document, status=status)
        for operation in ("imessage_import", "conversation_import", "retired_method"):
            self.retained_run(document, operation=operation)

        self.login("viewer")
        query = {"batch": batch.pk, "kind": "document", "status": "partial"}
        for headers in ({}, {"X-Pandect-Live": "1"}):
            with self.subTest(headers=headers):
                response = self.client.get(
                    self.processing_url(), query, headers=headers
                )
                self.assertContains(response, 'id="processing-analysis-runs"')
                self.assertContains(response, "Analysis run counts")
                self.assertEqual(
                    [run.pk for run in response.context["page_obj"]], [rerun.pk]
                )
                self.assertEqual(response.context["analysis_run_total"], 8)
                self.assertEqual(
                    response.context["analysis_run_counts"],
                    [
                        {"label": "Extract identifiers and dates", "count": 4},
                        {"label": "Extract document text", "count": 2},
                        {"label": "Read text in images", "count": 2},
                    ],
                )

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
        self.assertEqual(len(response.context["import_history"]), 1)
        self.assertEqual(response.context["import_history"].paginator.count, 26)
        self.assertEqual(
            {row.pk for row in response.context["import_history"]},
            {imports[0].pk},
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
        self.assertContains(response, "Run local checks")

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
        for role in ("viewer", "outsider"):
            self.login(role)
            for headers in ({}, {"X-Pandect-Live": "1"}):
                with self.subTest(role=role, headers=headers):
                    response = self.client.get(self.processing_url(), headers=headers)
                    self.assertNotContains(
                        response, 'id="processing-analysis-runs"', status_code=404
                    )

    def test_processing_get_is_read_only_and_validates_history_filters(self):
        source = self.document()
        self.retained_run(source, status="queued")
        self.login("owner")
        before = (AnalysisRun.objects.count(), CaseAnalysisBatch.objects.count())
        response = self.client.get(
            self.processing_url(), {"kind": "untrusted", "status": "invented"}
        )
        self.assertEqual(response.context["history_filters"], {})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        self.assertEqual(response.context["active_runs"].paginator.count, 1)
        self.assertEqual(
            (AnalysisRun.objects.count(), CaseAnalysisBatch.objects.count()), before
        )
        self.assertContains(response, "Run local checks")
        fragment = self.client.get(self.processing_url(), HTTP_X_PANDECT_LIVE="1")
        for result in (response, fragment):
            self.assertContains(result, 'id="processing-analysis-runs"')
            self.assertNotContains(result, "Analysis run counts")
            self.assertEqual(result.context["analysis_run_total"], 0)

    def test_imports_ready_for_review_remain_visible_with_current_work(self):
        ready = self.vehicle_import(status="review", title="Locations ready for review")
        self.login("editor")
        response = self.client.get(self.processing_url())
        self.assertEqual(
            [item.pk for item in response.context["import_reviews"]], [ready.pk]
        )
        self.assertContains(response, "Ready to review")
        self.assertContains(response, "Needs attention")
        self.assertEqual(response.context["active_imports"].paginator.count, 0)
        self.assertContains(
            response,
            reverse("geo:vehicle_import_review", args=[self.case.pk, ready.pk]),
        )

    def test_needs_attention_links_current_stopped_preparation_and_imports(self):
        owner = self.users["owner"]
        stopped = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=owner, status="failed"
        )
        older = CaseAnalysisBatch.objects.create(
            case=self.case,
            requested_by=owner,
            status="failed",
            scope={
                "type": "reprocess",
                "mode": "local",
                "kinds": ["text"],
                "executions": {},
            },
        )
        CaseAnalysisBatch.objects.create(
            case=self.case,
            requested_by=owner,
            status="complete",
            scope=older.scope | {"executions": {"model": "reviewed route"}},
        )
        stopped_import = self.vehicle_import(status="failed", title="Stopped locations")
        state = VehicleTripState.objects.create(
            case=self.case, vehicle=self.vehicle, revision=2
        )
        obsolete_trip = VehicleTripGeneration.objects.create(
            case=self.case,
            vehicle=self.vehicle,
            state=state,
            revision=1,
            method_version=METHOD_VERSION,
            requested_by=owner,
            status="failed",
        )
        stopped_trip = VehicleTripGeneration.objects.create(
            case=self.case,
            vehicle=self.vehicle,
            state=state,
            revision=2,
            method_version=METHOD_VERSION,
            requested_by=owner,
            status="failed",
        )
        self.login("editor")
        response = self.client.get(self.processing_url(), HTTP_X_PANDECT_LIVE="1")
        self.assertEqual(
            [item.pk for item in response.context["failed_batches"]], [stopped.pk]
        )
        self.assertEqual(
            [item.pk for item in response.context["failed_imports"]],
            [stopped_import.pk],
        )
        self.assertEqual(
            [item.pk for item in response.context["failed_trips"]], [stopped_trip.pk]
        )
        self.assertNotIn(obsolete_trip, response.context["failed_trips"])
        self.assertContains(response, "Review stopped local checks")
        self.assertContains(response, "Retry trip processing")
        selected = self.client.get(self.processing_url(), {"batch": stopped.pk})
        self.assertContains(selected, "Review local check actions")
        self.login("viewer")
        readonly = self.client.get(self.processing_url(), {"batch": stopped.pk})
        self.assertNotContains(readonly, "Retry trip processing")
        self.assertNotContains(readonly, "Review local check actions")

    def test_newer_session_summary_attempt_supersedes_only_the_same_selection(self):
        owner = self.users["owner"]
        people = sorted([str(self.person.pk), str(uuid4())])
        scope = {
            "type": "instance_summaries",
            "people": people,
            "instances": [str(uuid4()), str(uuid4())],
            "backend": "local",
        }
        stopped = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=owner, status="failed", scope=scope
        )
        other = CaseAnalysisBatch.objects.create(
            case=self.case,
            requested_by=owner,
            status="failed",
            scope=scope | {"people": sorted([str(self.person.pk), str(uuid4())])},
        )
        newer = CaseAnalysisBatch.objects.create(
            case=self.case,
            requested_by=owner,
            status="complete",
            scope=scope | {"instances": scope["instances"][1:], "backend": "gateway"},
        )

        self.login("viewer")
        response = self.client.get(self.processing_url())
        self.assertEqual(
            [item.pk for item in response.context["failed_batches"]],
            [other.pk, stopped.pk],
        )
        self.assertEqual(
            [item.pk for item in response.context["batches"]],
            [newer.pk, other.pk, stopped.pk],
        )
        CaseAnalysisBatch.objects.create(
            case=self.case,
            requested_by=owner,
            status="complete",
            scope=scope | {"backend": "gateway"},
        )
        response = self.client.get(self.processing_url())
        self.assertEqual(
            [item.pk for item in response.context["failed_batches"]], [other.pk]
        )

    def test_pending_message_reviews_exclude_old_formats_and_keep_the_page(self):
        document = self.document()
        method = OPERATIONS["imessage_import"]
        previews = [
            self.retained_run(
                document,
                operation="imessage_import",
                method_version=version,
                parameters={"phase": "preview"},
            )
            for version in ["1", *([method.version] * 26)]
        ]
        AnalysisResult.objects.bulk_create(
            [
                AnalysisResult(
                    case=self.case, run=run, data={"metrics": {"ready_rows": 1}}
                )
                for run in previews
            ]
        )
        legacy, first, *_ = previews
        self.login("viewer")
        overview = self.client.get(self.processing_url())
        self.assertEqual([run.pk for run in overview.context["page_obj"]], [legacy.pk])
        pending = self.client.get(
            self.processing_url(), {"reviews": "messages", "page": 2}
        )
        self.assertEqual([run.pk for run in pending.context["page_obj"]], [first.pk])
        query = {"reviews": "messages", "history_page": 2}
        processing = reverse("core:analysis:detail", args=[self.case.pk, first.pk])
        self.assertContains(
            pending, processing + "?reviews=messages&amp;history_page=2"
        )
        self.assertRedirects(
            self.client.get(processing, query),
            reverse(
                "phone:imessage_review", args=[self.case.pk, first.pk], query=query
            ),
            fetch_redirect_response=False,
        )

    def test_result_returns_to_its_own_batch_and_preserves_the_status_filter(self):
        document = self.document()
        batch = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.users["owner"]
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(document.pk)}],
            batch=batch,
        )
        self.login("viewer")
        processing = self.processing_url()
        detail = reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        response = self.client.get(processing, {"batch": batch.pk, "status": "queued"})
        self.assertContains(response, f"{detail}?status=queued&amp;batch={batch.pk}")
        result = self.client.get(detail, {"batch": batch.pk, "status": "queued"})
        self.assertContains(result, "Back to batch checks")
        self.assertEqual(
            result.context["back_to_analysis"],
            f"{processing}?status=queued&batch={batch.pk}#processing-history",
        )
        unrelated = self.client.get(detail, {"batch": uuid4()})
        self.assertEqual(unrelated.context["back_to_analysis"], processing)

    def test_history_opens_the_saved_result_and_returns_to_evidence(self):
        document = self.document()
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "native_text",
            [{"kind": "document", "id": str(document.pk)}],
        )
        process_run(run.pk)
        self.login("viewer")
        processing = self.processing_url()
        overview = self.client.get(processing)
        self.assertContains(overview, "Browse findings")
        self.assertNotContains(overview, 'id="processing-history" open')
        history = self.client.get(processing, {"history": "1"})
        self.assertContains(history, 'id="processing-history" open')
        self.assertContains(history, "Saved result")
        detail = reverse("core:analysis:detail", args=[self.case.pk, run.pk])
        self.assertContains(history, f'href="{detail}?history=1"')
        result = self.client.get(detail, {"history": "1"})
        self.assertEqual(result.context["result"].pk, run.result.pk)
        self.assertEqual(
            result.context["back_to_analysis"],
            f"{processing}?history=1#processing-history",
        )
        evidence = reverse(
            "core:evidence:detail", args=[self.case.pk, "document", document.pk]
        )
        self.assertContains(result, f'href="{evidence}#evidence-findings-heading"')

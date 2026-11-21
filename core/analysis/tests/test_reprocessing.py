"""Bulk methods preserve scope, approval, history, and bounded recovery."""

from unittest.mock import patch
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.http import Http404
from django.test import TestCase, override_settings
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.execution import requires_inference
from core.analysis.operations import OPERATIONS
from core.analysis.reprocessing import (
    CONTEXT_REQUIRED_OPERATIONS,
    REPROCESS_OPERATIONS,
    start_reprocessing,
)
from core.analysis.services import process_run
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_execution import LOCAL
from core.analysis.tests.test_gateway import CONFIG
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from text.models import TextEvidence


@override_settings(PANDECT_LOCAL_AI=LOCAL, PANDECT_AI_GATEWAY=CONFIG)
class ReprocessingTests(EvidenceViewTestMixin, TestCase):
    def test_every_available_operation_has_an_explicit_bulk_or_context_path(self):
        self.assertEqual(
            set(OPERATIONS),
            set(REPROCESS_OPERATIONS) | set(CONTEXT_REQUIRED_OPERATIONS),
        )
        self.assertFalse(set(REPROCESS_OPERATIONS) & set(CONTEXT_REQUIRED_OPERATIONS))

    def start(self, *, role="owner", kinds=("text",), approved=False, **kwargs):
        if approved:
            external_grant_kwargs(self.case, "content_events")
        return start_reprocessing(
            self.users[role],
            self.case.pk,
            submission_id=kwargs.pop("submission_id", uuid4()),
            kinds=list(kinds),
            **kwargs,
        )

    def drain(self, batch):
        for _ in range(100):
            prepare_case_analysis_batch(str(batch.pk))
            batch.refresh_from_db()
            if batch.status != "queued":
                self.assertEqual(batch.status, "complete", batch.error_code)
                return
        self.fail("Reprocessing did not finish preparing its bounded groups")

    def test_local_includes_deterministic_and_private_ai_methods(self):
        self.text()
        self.text()
        batch, _ = self.start()
        self.drain(batch)
        runs = list(batch.runs.all())
        self.assertTrue(
            {
                "entities",
                "style_profiles",
                "copied_text",
                "text_topics",
                "content_events",
            }
            <= {run.operation for run in runs}
        )
        self.assertFalse(any(run.external_processing for run in runs))
        models = [
            run for run in runs if requires_inference(run.operation, run.parameters)
        ]
        self.assertTrue(models)
        self.assertTrue(all(run.parameters["backend"] == "local" for run in models))

    def test_owner_approval_enables_both_models_and_one_deterministic_lane(self):
        self.text()
        batch, _ = self.start(approved=True)
        self.drain(batch)
        runs = list(batch.runs.all())
        models = [
            run for run in runs if requires_inference(run.operation, run.parameters)
        ]
        self.assertEqual(
            {run.parameters["backend"] for run in models}, {"local", "gateway"}
        )
        for run in models:
            self.assertEqual(
                run.external_processing, run.parameters["backend"] == "gateway"
            )
        self.assertEqual(batch.runs.filter(operation="entities").count(), 1)

    def test_selected_active_sources_only_and_original_results_remain(self):
        active = self.text()
        archived = self.text(
            is_archived=True,
            archive_changed_at=timezone.now(),
            archive_changed_by=self.users["owner"],
        )
        foreign = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Separate case text",
            body="Synthetic separate material",
        )
        batch, _ = self.start()
        newer = self.text()
        self.drain(batch)
        identifiers = batch.runs.get(operation="entities")
        initial_count = batch.runs.count()
        self.assertEqual(
            {entry["evidence_id"] for entry in identifiers.inputs}, {str(active.pk)}
        )
        self.assertFalse(
            {str(archived.pk), str(foreign.pk), str(newer.pk)}
            & {entry["evidence_id"] for run in batch.runs.all() for entry in run.inputs}
        )
        process_run(identifiers.pk)
        identifiers.refresh_from_db()
        self.assertEqual(batch.runs.count(), initial_count)
        original_result_id = identifiers.result.pk
        later, _ = self.start()
        self.drain(later)
        identifiers.refresh_from_db()
        self.assertEqual(identifiers.result.pk, original_result_id)

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=2)
    def test_groups_cover_every_source_with_explicit_comparison_limits(self):
        sources = [self.text() for _ in range(5)]
        batch, _ = self.start()
        self.drain(batch)
        runs = list(batch.runs.filter(operation="entities"))
        self.assertTrue(all(1 <= len(run.inputs) <= 2 for run in batch.runs.all()))
        self.assertEqual(
            {entry["evidence_id"] for run in runs for entry in run.inputs},
            {str(item.pk) for item in sources},
        )
        process_run(runs[0].pk)
        runs[0].refresh_from_db()
        self.assertTrue(runs[0].result.data["warnings"])

    def test_submission_is_idempotent_and_cannot_change_scope(self):
        self.text()
        batch, created = self.start()
        self.assertTrue(created)
        repeated, created = self.start(submission_id=batch.pk)
        self.assertFalse(created)
        self.assertEqual(repeated.pk, batch.pk)
        with self.assertRaises(ValidationError):
            self.start(submission_id=batch.pk, kinds=("document",))
        with self.assertRaises(ValidationError):
            self.start(submission_id=batch.pk, role="editor")

    @override_settings(PANDECT_ANALYSIS_MAX_SOURCES=1)
    def test_deleted_cursor_source_does_not_stop_already_requested_preparation(self):
        earlier = self.text()
        retained = self.text()
        batch, _ = self.start()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
        batch.refresh_from_db()
        self.assertEqual(batch.source_cursor, earlier.pk)
        TextEvidence.objects.filter(pk=batch.source_cursor).delete()
        self.drain(batch)
        self.assertEqual(batch.processed_sources, 2)
        self.assertTrue(
            batch.runs.filter(
                operation="entities",
                inputs__contains=[{"evidence_id": str(retained.pk)}],
            ).exists()
        )

    def test_invalid_selection_is_rejected_in_backend(self):
        for values in (
            {"kinds": ()},
            {"kinds": ("invented",)},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.start(**values)
        self.assertFalse(AnalysisRun.objects.exists())

    def test_current_access_and_verified_account_are_required_during_preparation(self):
        self.text()
        with self.assertRaises(PermissionDenied):
            self.start(role="viewer")
        with self.assertRaises(Http404):
            self.start(role="outsider")
        batch, _ = self.start(role="editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        prepare_case_analysis_batch(batch.pk)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "failed")
        self.assertFalse(batch.runs.exists())
        unverified, _ = self.start()
        EmailAddress.objects.filter(user=self.users["owner"]).update(verified=False)
        prepare_case_analysis_batch(unverified.pk)
        unverified.refresh_from_db()
        self.assertEqual(unverified.status, "failed")
        self.assertFalse(unverified.runs.exists())

    def test_execution_drift_cannot_queue_unreviewed_destinations(self):
        self.text()
        batch, _ = self.start()
        configuration = settings.PANDECT_LOCAL_AI
        with self.settings(
            PANDECT_LOCAL_AI=configuration
            | {"text": configuration.get("text", {}) | {"runtime": "different-runtime"}}
        ):
            self.drain(batch)
        self.assertEqual(batch.status, "complete")
        self.assertTrue(batch.runs.filter(operation="entities").exists())
        self.assertFalse(batch.runs.filter(operation="content_events").exists())
        self.assertGreater(batch.unavailable_sources, 0)

    def test_document_extraction_queues_exact_text_followups_in_same_batch(self):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic text document",
            file=ContentFile(b"Contact synthetic@example.test", name="source.txt"),
        )
        batch, _ = self.start(kinds=("document",), approved=True)
        self.drain(batch)
        extraction = batch.runs.get(operation="native_text")
        process_run(extraction.pk)
        extraction.refresh_from_db()
        self.assertEqual(extraction.status, "complete", extraction.error_code)
        followups = list(batch.runs.filter(previous_run=extraction))
        self.assertTrue(followups)
        self.assertTrue(
            {"entities", "content_events"} <= {run.operation for run in followups}
        )
        for run in followups:
            self.assertEqual(run.inputs[0]["evidence_id"], str(document.pk))
            self.assertEqual(run.inputs[0]["result_id"], str(extraction.result.pk))
        models = [
            run
            for run in followups
            if requires_inference(run.operation, run.parameters)
        ]
        self.assertEqual(
            {run.parameters["backend"] for run in models}, {"local", "gateway"}
        )

    def test_one_method_preparation_failure_preserves_other_methods(self):
        from core.analysis.reprocessing import start_run

        self.text()

        def fail_entities(actor, case_id, operation, *args, **kwargs):
            if operation == "entities":
                raise ValidationError("Synthetic unavailable method")
            return start_run(actor, case_id, operation, *args, **kwargs)

        batch, _ = self.start()
        with patch("core.analysis.reprocessing.start_run", side_effect=fail_entities):
            self.drain(batch)
        self.assertFalse(batch.runs.filter(operation="entities").exists())
        self.assertTrue(batch.runs.filter(operation="content_events").exists())
        self.assertGreater(batch.unavailable_sources, 0)

    def test_settings_drift_during_extraction_preserves_independent_routes(
        self,
    ):
        DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic extraction before settings change",
            file=ContentFile(b"Contact synthetic@example.test", name="source.txt"),
        )
        batch, _ = self.start(kinds=("document",), approved=True)
        self.drain(batch)
        extraction = batch.runs.get(operation="native_text")
        configuration = settings.PANDECT_LOCAL_AI
        with self.settings(
            PANDECT_LOCAL_AI=configuration
            | {"text": configuration.get("text", {}) | {"runtime": "changed-runtime"}}
        ):
            process_run(extraction.pk)
        extraction.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(extraction.status, "complete", extraction.error_code)
        self.assertTrue(extraction.result.passages.exists())
        followups = batch.runs.filter(previous_run=extraction)
        self.assertTrue(followups.filter(operation="entities").exists())
        self.assertTrue(
            followups.filter(
                operation="content_events", external_processing=True
            ).exists()
        )
        self.assertFalse(
            followups.filter(
                operation="content_events", external_processing=False
            ).exists()
        )
        self.assertGreater(batch.unavailable_sources, 0)

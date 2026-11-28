"""Case authorization, retained provenance and atomic processing lifecycle checks."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, close_old_connections, transaction
from django.http import Http404
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django_tasks_db.models import DBTaskResult

from cases.models import Case, CaseMembership
from core.analysis.address_estimates import require_current_geocode_reviews
from core.analysis.errors import AnalysisError
from core.analysis.operations import get_operation
from core.analysis.result_validation import validate_output
from core.analysis.services import cancel_run, start_run
from core.analysis.source_resolution import resolve_inputs, resolve_snapshots
from core.analysis.source_selection import snapshot_sources
from core.analysis.sources import Source, output
from core.analysis.tests.testing import external_grant_kwargs
from core.analysis.worker import process_run
from core.models import (
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
    ExtractedPassage,
    GatewayRequest,
)
from core.tests.test_tasks import drain_queue
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from geo.models import LocationEvidence
from geo.tests.testing import LOCAL_GEOCODER
from phone.models import CallEvidenceAttachment, PhoneCallEvidence
from text.analysis.deterministic import extract_entities
from text.models import TextEvidence


class ProcessingTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.original = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic identifier source",
            body="Write to source@example.test.",
        )

    def selection(self, item=None, **extra):
        return {"kind": "text", "id": str((item or self.original).pk), **extra}

    def start(self, role="owner", **kwargs):
        return start_run(
            self.users[role], self.case.pk, "entities", [self.selection()], **kwargs
        )

    def complete(self, role="owner"):
        run, _ = self.start(role)
        process_run(str(run.pk))
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        return run, run.result

    def test_owners_and_editors_enqueue_private_uuid_only_and_viewers_cannot_start(
        self,
    ):
        for role in ("owner", "editor"):
            run, created = self.start(role)
            self.assertTrue(created)
            self.assertEqual(run.requested_by, self.users[role])
            self.assertEqual(run.status, "queued")
            self.assertEqual(run.inputs[0]["sha256"], self.original.sha256)
            queued = DBTaskResult.objects.filter(
                task_path="core.analysis.tasks.process_analysis"
            ).get(args_kwargs={"args": [str(run.pk)], "kwargs": {}})
            self.assertNotIn("source@example.test", str(queued.args_kwargs))
        with self.assertRaises(PermissionDenied):
            self.start("viewer")
        with self.assertRaises(Http404):
            self.start("outsider")
        self.users["outsider"].is_superuser = True
        self.users["outsider"].save()
        with self.assertRaises(Http404):
            self.start("outsider")

    def test_submission_retry_returns_same_run_and_single_queue_record(self):
        identifier = uuid4()
        first, created = self.start(submission_id=identifier)
        second, repeated = self.start(submission_id=identifier)
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(DBTaskResult.objects.count(), 1)
        with self.assertRaises(ValidationError):
            self.start(submission_id=identifier, parameters={"different": True})
        editor_run, created = self.start("editor", submission_id=identifier)
        self.assertTrue(created)
        self.assertNotEqual(editor_run.pk, first.pk)
        self.assertEqual(editor_run.requested_by, self.users["editor"])
        self.assertEqual(AnalysisRun.objects.count(), 2)
        self.assertEqual(DBTaskResult.objects.count(), 2)

    def test_retry_identity_includes_previous_run(self):
        previous, _ = self.start()
        other, _ = self.start()
        identifier = uuid4()
        self.start(submission_id=identifier, previous_run=previous)
        with self.assertRaises(ValidationError):
            self.start(submission_id=identifier, previous_run=other)

    def test_previous_processing_cannot_cross_case_history(self):
        foreign = AnalysisRun.objects.create(
            case=self.other_case,
            requested_by=self.users["outsider"],
            operation="entities",
            method_version="1",
        )
        with self.assertRaises(Http404):
            self.start(previous_run=foreign)
        with self.assertRaises(ValidationError):
            AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.users["owner"],
                operation="entities",
                method_version="1",
                previous_run=foreign,
            )

    def test_inconsistent_history_reference_is_concealed_on_private_read_routes(self):
        foreign = AnalysisRun.objects.create(
            case=self.other_case,
            requested_by=self.users["outsider"],
            operation="entities",
            method_version="1",
        )
        own, _ = self.start()
        # Maintenance writes can bypass saves; readers still scope history.
        AnalysisRun.objects.filter(pk=own.pk).update(previous_run=foreign)
        for role in ("owner", "editor", "viewer"):
            self.client.force_login(self.users[role])
            for route in ("detail", "download"):
                url = reverse(
                    f"core:analysis:{route}",
                    kwargs={"case_id": self.case.pk, "run_id": own.pk},
                )
                with self.subTest(role=role, route=route):
                    self.assertEqual(self.client.get(url).status_code, 404)

    def test_invalid_parameters_fail_validation_without_queueing(self):
        for parameters in (
            [],
            ["settings"],
            {"threshold": float("nan")},
            {"nested": object()},
        ):
            with self.subTest(parameters_type=type(parameters)):
                with self.assertRaises(ValidationError):
                    self.start(parameters=parameters)
        self.assertEqual(AnalysisRun.objects.count(), 0)
        self.assertEqual(DBTaskResult.objects.count(), 0)

    def test_bad_selection_duplicate_source_and_cross_case_relationships_are_rejected(
        self,
    ):
        foreign = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Other case source",
            body="Private other case",
        )
        for selections in (
            [self.selection(foreign)],
            [self.selection(), self.selection()],
        ):
            with (
                self.subTest(selections=selections),
                self.assertRaises((Http404, ValidationError)),
            ):
                start_run(self.users["owner"], self.case.pk, "entities", selections)
        with self.assertRaises(ValidationError):
            start_run(self.users["owner"], self.case.pk, "entities", [])
        with self.assertRaises(ValidationError):
            start_run(
                self.users["owner"], self.case.pk, "entities", [self.selection()] * 501
            )
        self.assertEqual(AnalysisRun.objects.count(), 0)

    def test_analysis_rejects_excess_original_text_before_hydrating_sources(self):
        texts = [
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Synthetic large text {index}",
                body="é" * 600_000,
            )
            for index in range(2)
        ]
        selections = [self.selection(item) for item in texts]
        patch_target = "core.analysis.source_resolution.resolve_snapshots"
        with (
            patch(patch_target) as hydrate,
            self.assertRaisesMessage(ValidationError, "exceed 1,000,000 characters"),
        ):
            start_run(self.users["owner"], self.case.pk, "entities", selections)
        hydrate.assert_not_called()
        self.assertFalse(AnalysisRun.objects.exists())

        # A queued version may predate admission checks. It still fails safely.
        run, _ = self.start()
        retained = snapshot_sources(self.case, selections)
        AnalysisRun.objects.filter(pk=run.pk).update(inputs=retained)
        with patch(patch_target) as hydrate:
            self.assertEqual(process_run(run.pk), "source_material_limit")
        hydrate.assert_not_called()
        self.assertFalse(AnalysisResult.objects.exists())

    def test_submission_resolves_content_once(self):
        with patch(
            "core.analysis.source_resolution.resolve_snapshots", wraps=resolve_snapshots
        ) as hydrate:
            self.start()
        self.assertEqual(hydrate.call_count, 1)

    def test_append_only_call_attachment_source_is_bound_to_exact_parent_case_and_hash(
        self,
    ):
        first = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="First",
            sender=self.person,
            recipient=self.person,
        )
        second = PhoneCallEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Second",
            sender=self.person,
            recipient=self.person,
        )
        attachment = CallEvidenceAttachment.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            phone_call=first,
            transcript="Synthetic transcript",
        )
        with self.assertRaises(Http404):
            start_run(
                self.users["owner"],
                self.case.pk,
                "entities",
                [
                    {
                        "kind": "phone-call",
                        "id": str(second.pk),
                        "attachment_id": str(attachment.pk),
                    }
                ],
            )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [
                {
                    "kind": "phone-call",
                    "id": str(first.pk),
                    "attachment_id": str(attachment.pk),
                }
            ],
        )
        sources = resolve_inputs(run, self.case)
        self.assertEqual(sources[0].text, attachment.transcript)
        self.assertEqual(run.inputs[0]["sha256"], attachment.sha256)
        CallEvidenceAttachment.objects.filter(pk=attachment.pk).update(sha256="f" * 64)
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            resolve_inputs(run, self.case)

    def test_revoked_downgraded_inactive_and_unverified_actors_cannot_publish(self):
        for condition in ("revoked", "downgraded", "inactive", "unverified"):
            with self.subTest(condition=condition):
                membership = CaseMembership.objects.get(
                    case=self.case, user=self.users["editor"]
                )
                actor = self.users["editor"]
                run, _ = self.start("editor")
                if condition == "revoked":
                    membership.delete()
                elif condition == "downgraded":
                    membership.role = "viewer"
                    membership.save()
                elif condition == "inactive":
                    actor.is_active = False
                    actor.save()
                else:
                    EmailAddress.objects.filter(user=actor).update(verified=False)
                self.assertEqual(process_run(str(run.pk)), "access_removed")
                run.refresh_from_db()
                self.assertEqual(run.status, "failed")
                self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
                CaseMembership.objects.update_or_create(
                    case=self.case, user=actor, defaults={"role": "editor"}
                )
                actor.is_active = True
                actor.save()
                EmailAddress.objects.filter(user=actor).update(verified=True)

    def test_source_changes_prevent_processing_and_plain_text_byte_changes_are_checked(
        self,
    ):
        run, _ = self.start()
        TextEvidence.objects.filter(pk=self.original.pk).update(
            body="Changed through trusted maintenance"
        )
        self.assertEqual(process_run(str(run.pk)), "source_changed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_duplicate_delivery_publishes_once_and_new_runs_retain_history(self):
        run, result = self.complete()
        self.assertEqual(process_run(str(run.pk)), "complete")
        self.assertEqual(AnalysisResult.objects.filter(run=run).count(), 1)
        run.refresh_from_db()
        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.progress, 100)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        repeated, _ = self.start(previous_run=run)
        process_run(str(repeated.pk))
        self.assertEqual(AnalysisResult.objects.count(), 2)
        self.assertEqual(repeated.previous_run_id, run.pk)
        self.assertEqual(result.provenance["source_inputs"], run.inputs)
        self.assertEqual(result.provenance["method_version"], run.method_version)
        self.assertEqual(result.data["items"][0]["text"], "source@example.test")

    def test_worker_stages_track_source_checks_execution_and_publication(self):
        run, _ = self.start()
        self.assertEqual(run.progress_label, "Queued")
        observed = []

        def observe_stage():
            run.refresh_from_db()
            observed.append(
                (run.progress_label, AnalysisResult.objects.filter(run=run).exists())
            )

        def observe_sources(*args, **kwargs):
            observe_stage()
            return resolve_inputs(*args, **kwargs)

        def observe_analysis(sources, parameters):
            observe_stage()
            return extract_entities(sources, parameters)

        with (
            patch(
                "core.analysis.source_resolution.resolve_inputs",
                side_effect=observe_sources,
            ),
            patch(
                "core.analysis.worker.get_operation",
                return_value=SimpleNamespace(
                    function=observe_analysis, version=run.method_version
                ),
            ),
        ):
            process_run(run.pk)
        self.assertEqual(
            observed,
            [
                ("Checking sources", False),
                ("Running analysis", False),
                ("Saving results", False),
            ],
        )
        run.refresh_from_db()
        self.assertEqual(run.progress_label, "Complete")
        self.assertTrue(AnalysisResult.objects.filter(run=run).exists())

    def test_cancellation_before_execution_and_before_publication_never_creates_result(
        self,
    ):
        run, _ = self.start()
        cancel_run(self.users["owner"], self.case.pk, run.pk)
        self.assertEqual(process_run(str(run.pk)), "cancelled")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        second, _ = self.start()

        def cancelled_operation(sources, parameters):
            cancel_run(self.users["editor"], self.case.pk, second.pk)
            return extract_entities(sources, parameters)

        with patch(
            "core.analysis.worker.get_operation",
            return_value=SimpleNamespace(
                function=cancelled_operation, version=second.method_version
            ),
        ):
            self.assertEqual(process_run(str(second.pk)), "processing_cancelled")
        second.refresh_from_db()
        self.assertEqual(second.status, "cancelled")
        self.assertEqual(second.stage, AnalysisRun.Stage.ANALYSIS)
        self.assertEqual(second.progress_label, "Cancelled")
        self.assertFalse(AnalysisResult.objects.filter(run=second).exists())

    def test_membership_removal_during_calculation_prevents_publication(self):
        run, _ = self.start("editor")

        def revoke(sources, parameters):
            CaseMembership.objects.filter(
                case=self.case, user=self.users["editor"]
            ).delete()
            return extract_entities(sources, parameters)

        with patch(
            "core.analysis.worker.get_operation",
            return_value=SimpleNamespace(function=revoke, version=run.method_version),
        ):
            self.assertEqual(process_run(str(run.pk)), "access_removed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_retained_models_reject_replacement_and_cross_case_children(self):
        run, result = self.complete()
        run.parameters = {"overwrite": True}
        with self.assertRaises(ValidationError):
            run.save()
        result.data = {"summary": "Replacement", "items": []}
        with self.assertRaises(ValidationError):
            result.save()
        review = AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="unresolved",
        )
        review.comment = "Replacement"
        with self.assertRaises(ValidationError):
            review.save()
        for model, values in (
            (AnalysisResult, {"run": run, "data": {}}),
            (
                AnalysisReview,
                {
                    "result": result,
                    "reviewed_by": self.users["owner"],
                    "decision": "unresolved",
                },
            ),
            (
                GatewayRequest,
                {"run": run, "model": "@cf/test/model", "request_bytes": 2},
            ),
            (
                ExtractedPassage,
                {
                    "result": result,
                    "kind": "text",
                    "evidence_id": self.original.pk,
                    "evidence_sha256": self.original.sha256,
                    "text": "derived",
                },
            ),
        ):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.objects.create(case=self.other_case, **values)
        for value in (run.pk, result.pk, review.pk):
            self.assertEqual(value.version, 4)

    def test_database_bounds_cannot_accept_invalid_progress_or_retry_count(self):
        run, _ = self.start()
        for values in ({"progress": 101}, {"attempts": 4}):
            with (
                self.subTest(values=values),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                AnalysisRun.objects.filter(pk=run.pk).update(**values)

    def test_derived_text_requires_matching_same_case_original_and_prior_source(self):
        prior, _ = self.complete()
        current, _ = self.start()
        current.inputs[0]["result_id"] = str(prior.result.pk)
        resolved = resolve_inputs(current, self.case)
        self.assertEqual(resolved[0].extracted_text, "source@example.test")
        foreign_text = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Different case",
            body="Other",
        )
        foreign, _ = start_run(
            self.users["outsider"],
            self.other_case.pk,
            "entities",
            [self.selection(foreign_text)],
        )
        process_run(str(foreign.pk))
        current.inputs[0]["result_id"] = str(foreign.result.pk)
        with self.assertRaisesMessage(AnalysisError, "source_unavailable"):
            resolve_inputs(current, self.case)

    def test_self_referential_derived_input_is_rejected(self):
        run, result = self.complete()
        run.inputs[0]["result_id"] = str(result.pk)
        with self.assertRaisesMessage(AnalysisError, "invalid_source_chain"):
            resolve_inputs(run, self.case)

    def test_derived_sources_load_shared_history_once_per_resolution(self):
        originals = [self.original] + [
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Synthetic source {index}",
                body=f"Source {index}: example{index}@example.test",
            )
            for index in range(4)
        ]
        prior, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [self.selection(item) for item in originals],
        )
        process_run(str(prior.pk))
        result_id = str(prior.result.pk)
        current = AnalysisRun(
            case=self.case,
            inputs=[entry | {"result_id": result_id} for entry in prior.inputs],
        )
        # Three preflight queries measure the material, then original and result
        # content are each loaded once.
        with self.assertNumQueries(5):
            resolved = resolve_inputs(current, self.case)
        self.assertEqual(len(resolved), len(originals))
        self.assertTrue(all(source.extracted_text for source in resolved))

        # A later resolution must reload even history outside the new selection.
        current.inputs = current.inputs[:1]
        TextEvidence.objects.filter(pk=originals[-1].pk).update(sha256="0" * 64)
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            resolve_inputs(current, self.case)

    def test_processing_resolves_inputs_once_before_work_and_again_at_publication(self):
        run, _ = self.start()
        with patch(
            "core.analysis.source_resolution.resolve_inputs", wraps=resolve_inputs
        ) as resolve:
            process_run(str(run.pk))
        self.assertEqual(resolve.call_count, 2)

    def test_multiple_result_versions_check_text_integrity_once_but_refresh_later(self):
        first, _ = self.complete()
        second, _ = self.complete()
        current, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "entities",
            [self.selection(result_id=str(run.result.pk)) for run in (first, second)],
        )
        with patch(
            "core.analysis.source_resolution.hashlib.sha256", wraps=hashlib.sha256
        ) as digest:
            self.assertEqual(len(resolve_inputs(current, self.case)), 2)
        self.assertEqual(digest.call_count, 1)
        # Reusing a verified original must not skip each selection's snapshot hash.
        current.inputs[1]["sha256"] = "0" * 64
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            resolve_inputs(current, self.case)
        current.inputs[1]["sha256"] = self.original.sha256
        TextEvidence.objects.filter(pk=self.original.pk).update(
            body="Changed by maintenance"
        )
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            resolve_inputs(current, self.case)

    def test_multiple_result_versions_check_storage_once_but_detect_later_missing_file(
        self,
    ):
        document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic document",
            file=ContentFile(b"Retained source text", name="source.txt"),
        )
        selections = []
        for version in range(2):
            source_id = str(uuid4())
            prior = AnalysisRun.objects.create(
                case=self.case,
                requested_by=self.users["owner"],
                operation="native_text",
                method_version="1",
                status="complete",
                inputs=[
                    {
                        "id": source_id,
                        "kind": "document",
                        "evidence_id": str(document.pk),
                        "sha256": document.sha256,
                    }
                ],
            )
            result = AnalysisResult.objects.create(
                case=self.case,
                run=prior,
                data=output(
                    "Retained text",
                    items=[
                        {
                            "label": f"Version {version}",
                            "text": "Retained source text",
                            "source_ids": [source_id],
                        }
                    ],
                ),
            )
            selections.append(
                {
                    "kind": "document",
                    "id": str(document.pk),
                    "result_id": str(result.pk),
                }
            )
        current, _ = start_run(
            self.users["owner"], self.case.pk, "entities", selections
        )
        with patch.object(
            document.file.storage, "exists", wraps=document.file.storage.exists
        ) as exists:
            self.assertEqual(len(resolve_inputs(current, self.case)), 2)
        self.assertEqual(exists.call_count, 1)
        document.file.storage.delete(document.file.name)
        with self.assertRaisesMessage(AnalysisError, "source_unavailable"):
            resolve_inputs(current, self.case)

    def test_result_validation_rejects_forged_sources_and_out_of_bounds_anchors(self):
        source = Source(str(uuid4()), "text", self.original)
        valid = {"label": "Derived item", "text": "source", "source_ids": [source.id]}
        for anchors in (
            [{"source_id": str(uuid4())}],
            [{"source_id": source.id, "start": -1, "end": 3}],
            [{"source_id": source.id, "start_seconds": 3, "end_seconds": 1}],
            [{"source_id": source.id, "page": 101}],
            [{"source_id": source.id, "region": [0.5, 0.5, 0.1, 0.1]}],
        ):
            with self.subTest(anchors=anchors), self.assertRaises(AnalysisError):
                validate_output(
                    output("Synthetic", items=[valid | {"anchors": anchors}]), [source]
                )
        with self.assertRaisesMessage(AnalysisError, "invalid_anchor"):
            validate_output(
                output("Synthetic", items=[valid | {"source_ids": [str(uuid4())]}]),
                [source],
            )

    @override_settings(**(LOCAL_GEOCODER | {"PANDECT_GEOCODER_LOCAL": False}))
    def geocode_fixture(self, *, with_reported_position=False):
        observations = [
            LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Address {index}",
                address=f"{index} Synthetic Street",
                latitude=42 if with_reported_position and index == 1 else None,
                longitude=-78 if with_reported_position and index == 1 else None,
            )
            for index in (1, 2)
        ]
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "geocode_locations",
            [{"kind": "location", "id": str(value.pk)} for value in observations],
            **external_grant_kwargs(self.case, "geocode_locations"),
        )
        AnalysisRun.objects.filter(pk=run.pk).update(status="complete", progress=100)
        run.refresh_from_db()
        items = []
        for source_index, latitude in ((0, 43), (1, 45), (0, 44)):
            source_id = run.inputs[source_index]["id"]
            items.append(
                {
                    "label": "Geocode candidate",
                    "text": "Unresolved spatial interpretation",
                    "source_ids": [source_id],
                    "anchors": [{"source_id": source_id}],
                    "values": {"latitude": latitude, "longitude": -79},
                }
            )
        result = AnalysisResult.objects.create(
            case=self.case, run=run, data=output("Candidates", items=items)
        )
        return observations, run, result

    def test_geocode_review_is_candidate_specific_and_uses_global_finding_index(self):
        observations, prior, result = self.geocode_fixture(with_reported_position=True)
        selection = {
            "kind": "location",
            "id": str(observations[0].pk),
            "result_id": str(result.pk),
            "geocode_candidate": 2,
        }
        AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="accepted",
        )
        with self.assertRaisesMessage(AnalysisError, "geocode_review_required"):
            start_run(self.users["owner"], self.case.pk, "spatial_groups", [selection])
        AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="accepted",
            item_index=1,
        )
        with self.assertRaisesMessage(AnalysisError, "geocode_review_required"):
            start_run(self.users["owner"], self.case.pk, "spatial_groups", [selection])
        accepted = AnalysisReview.objects.create(
            reviewed_by=self.users["editor"],
            case=self.case,
            result=result,
            decision="accepted",
            comment="Checked candidate 2",
            item_index=2,
        )
        self.assertEqual(accepted.item_index, 2)
        current, _ = start_run(
            self.users["owner"], self.case.pk, "spatial_groups", [selection]
        )
        resolved = resolve_inputs(current, self.case)[0]
        self.assertEqual(resolved.derived_coordinates, (44.0, -79.0))
        prior_url = reverse("core:analysis:detail", args=[self.case.pk, prior.pk])
        self.assertEqual(resolved.url, prior_url + "?page=1&finding=3#finding-3")
        self.login("viewer")
        response = self.client.get(
            reverse("core:analysis:detail", args=[self.case.pk, current.pk])
        )
        self.assertContains(response, "Reported coordinates 42.000000, -78.000000")
        self.assertContains(response, "Address estimate 44.0, -79.0")
        for decision in ("rejected", "unresolved"):
            AnalysisReview.objects.create(
                reviewed_by=self.users["editor"],
                case=self.case,
                result=result,
                decision=decision,
                item_index=2,
            )
            with self.assertRaisesMessage(AnalysisError, "geocode_review_required"):
                resolve_inputs(current, self.case)
        self.assertEqual(result.reviews.filter(item_index=2).count(), 3)
        self.assertEqual(result.reviews.filter(item_index=1).count(), 1)

    def test_quality_can_compare_accepted_candidates_without_counting_them_as_samples(
        self,
    ):
        observations, _, result = self.geocode_fixture()
        for index in (0, 2):
            AnalysisReview.objects.create(
                reviewed_by=self.users["owner"],
                case=self.case,
                result=result,
                decision="accepted",
                item_index=index,
            )
        choices = [
            {
                "kind": "location",
                "id": str(observations[0].pk),
                "result_id": str(result.pk),
                "geocode_candidate": index,
            }
            for index in (0, 2)
        ]
        run, _ = start_run(
            self.users["owner"], self.case.pk, "location_quality", choices
        )
        process_run(str(run.pk))
        self.assertEqual(run.result.data["metrics"]["Unique original observations"], 1)
        self.assertEqual(run.result.data["metrics"]["Selected interpretations"], 2)
        with self.assertRaisesMessage(ValidationError, "Choose each source once"):
            start_run(
                self.users["owner"],
                self.case.pk,
                "location_quality",
                [choices[0], choices[0]],
            )
        AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="accepted",
            item_index=1,
        )
        sole_candidate = {
            "kind": "location",
            "id": str(observations[1].pk),
            "result_id": str(result.pk),
        }
        with self.assertRaisesMessage(ValidationError, "Choose each source once"):
            start_run(
                self.users["owner"],
                self.case.pk,
                "location_quality",
                [sole_candidate, sole_candidate | {"geocode_candidate": 1}],
            )
        movement, _ = start_run(
            self.users["owner"], self.case.pk, "spatial_groups", choices
        )
        process_run(str(movement.pk))
        movement.refresh_from_db()
        self.assertEqual(movement.status, "failed")
        self.assertFalse(AnalysisResult.objects.filter(run=movement).exists())

    def test_current_geocode_review_check_reuses_historical_resolution(self):
        observations, _, result = self.geocode_fixture()
        AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="accepted",
            item_index=0,
        )
        run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "spatial_groups",
            [
                {
                    "kind": "location",
                    "id": str(observations[0].pk),
                    "result_id": str(result.pk),
                    "geocode_candidate": 0,
                }
            ],
        )
        sources = resolve_inputs(run, self.case, require_current_review=False)
        with self.assertNumQueries(1):
            require_current_geocode_reviews(run, sources)
        AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="rejected",
            item_index=0,
        )
        with (
            self.assertNumQueries(1),
            self.assertRaisesMessage(AnalysisError, "geocode_review_required"),
        ):
            require_current_geocode_reviews(run, sources)
        # A later accepted review restores current usability without replacing
        # the recorded historical acceptance or resolved coordinates.
        AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="accepted",
            item_index=0,
        )
        require_current_geocode_reviews(run, sources)
        plain, _ = self.start()
        with self.assertNumQueries(0):
            require_current_geocode_reviews(
                plain, [Source("plain", "text", self.original)]
            )

    def test_geocode_selection_rejects_wrong_source_boolean_and_unknown_candidate(self):
        observations, prior, result = self.geocode_fixture()
        AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="accepted",
            item_index=0,
        )
        for candidate in (False, True, -1, 1, 5, "0"):
            selection = {
                "kind": "location",
                "id": str(observations[0].pk),
                "result_id": str(result.pk),
                "geocode_candidate": candidate,
            }
            with (
                self.subTest(candidate=candidate),
                self.assertRaises((AnalysisError, ValidationError)),
            ):
                start_run(
                    self.users["owner"], self.case.pk, "spatial_groups", [selection]
                )
        with self.assertRaisesMessage(AnalysisError, "ambiguous_geocode"):
            start_run(
                self.users["owner"],
                self.case.pk,
                "spatial_groups",
                [
                    {
                        "kind": "location",
                        "id": str(observations[0].pk),
                        "result_id": str(result.pk),
                    }
                ],
            )

    def test_boolean_anchor_values_are_rejected_as_malformed_measurements(self):
        source = Source(str(uuid4()), "text", self.original)
        for values in (
            {"start": False, "end": True},
            {"page": True},
            {"region": [False, False, True, True]},
            {"start_seconds": False, "end_seconds": True},
        ):
            item = {
                "label": "Synthetic",
                "text": "source",
                "source_ids": [source.id],
                "anchors": [{"source_id": source.id, **values}],
            }
            with (
                self.subTest(values=values),
                self.assertRaisesMessage(AnalysisError, "invalid_anchor"),
            ):
                validate_output(output("Synthetic", items=[item]), [source])

    def test_enqueue_failure_rolls_back_domain_run_and_submission_can_be_retried(self):
        identifier = uuid4()
        with patch(
            "core.analysis.tasks.process_analysis",
            SimpleNamespace(
                enqueue=Mock(side_effect=RuntimeError("Queue unavailable"))
            ),
        ):
            with self.assertRaises(RuntimeError):
                self.start(submission_id=identifier)
        self.assertEqual(AnalysisRun.objects.count(), 0)
        self.assertEqual(DBTaskResult.objects.count(), 0)
        run, created = self.start(submission_id=identifier)
        self.assertTrue(created)
        self.assertEqual(run.submission_id, identifier)

    def test_publication_failure_rolls_back_result_and_preserves_safe_failure(self):
        run, _ = self.start()
        create = AnalysisResult.objects.create

        def fail_after_insert(**kwargs):
            create(**kwargs)
            raise RuntimeError("Synthetic private publication failure")

        with patch(
            "core.analysis.worker.AnalysisResult.objects.create",
            side_effect=fail_after_insert,
        ):
            self.assertEqual(process_run(str(run.pk)), "processing_failed")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())
        self.assertEqual(DBTaskResult.objects.count(), 1)

    def test_run_creation_timestamp_is_retained_history(self):
        run, _ = self.start()
        run.created_at -= timedelta(days=1)
        with self.assertRaises(ValidationError):
            run.save()

    def test_revoked_actor_cannot_resume_submission(self):
        run, _ = self.complete("editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        with self.assertRaises(Http404):
            self.start("editor", submission_id=run.submission_id)

    def test_whole_case_deletion_cascades_results_and_preserves_other_cases(self):
        run, result = self.complete()
        AnalysisReview.objects.create(
            reviewed_by=self.users["owner"],
            case=self.case,
            result=result,
            decision="accepted",
        )
        GatewayRequest.objects.create(
            case=self.case, run=run, model="@cf/test/model", request_bytes=2
        )
        other_text = TextEvidence.objects.create(
            case=self.other_case,
            uploaded_by=self.users["outsider"],
            title="Other source",
            body="other@example.test",
        )
        other, _ = start_run(
            self.users["outsider"],
            self.other_case.pk,
            "entities",
            [self.selection(other_text)],
        )
        process_run(str(other.pk))
        self.case.delete()
        self.assertFalse(AnalysisRun.objects.filter(pk=run.pk).exists())
        self.assertFalse(AnalysisResult.objects.filter(pk=result.pk).exists())
        self.assertEqual(AnalysisReview.objects.count(), 0)
        self.assertEqual(GatewayRequest.objects.count(), 0)
        self.assertTrue(AnalysisResult.objects.filter(run=other).exists())

    def test_worker_rejects_unavailable_version_without_reinterpreting_inputs(
        self,
    ):
        run, _ = self.start()
        function = Mock()
        changed = SimpleNamespace(
            function=function, version=get_operation("entities").version + ".changed"
        )
        with patch("core.analysis.worker.get_operation", return_value=changed):
            self.assertEqual(process_run(str(run.pk)), "processing_version_unavailable")
        function.assert_not_called()
        self.assertFalse(AnalysisResult.objects.filter(run=run).exists())

    def test_parser_exception_content_is_not_retained_as_error_text(self):
        run, _ = self.start()

        def failed_operation(sources, parameters):
            raise RuntimeError("Synthetic sensitive source text and credentials")

        with patch(
            "core.analysis.worker.get_operation",
            return_value=SimpleNamespace(
                function=failed_operation, version=run.method_version
            ),
        ):
            self.assertEqual(process_run(str(run.pk)), "processing_failed")
        run.refresh_from_db()
        self.assertEqual(run.error_code, "processing_failed")
        self.assertNotIn("sensitive", str(run.__dict__))


class QueuePublicationTests(TransactionTestCase):
    def test_concurrent_identical_submissions_commit_one_run_and_one_task(self):
        user = get_user_model().objects.create_user(
            username="concurrent-processing-owner", email="concurrent@example.test"
        )
        EmailAddress.objects.create(
            user=user, email=user.email, verified=True, primary=True
        )
        case = Case.objects.create(owner=user, name="Synthetic concurrent processing")
        evidence = TextEvidence.objects.create(
            case=case,
            uploaded_by=user,
            title="Synthetic",
            body="concurrent@example.test",
        )
        identifier = uuid4()
        barrier = Barrier(2)

        def submit():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=user.pk)
                barrier.wait(timeout=10)
                run, created = start_run(
                    actor,
                    case.pk,
                    "entities",
                    [{"kind": "text", "id": str(evidence.pk)}],
                    submission_id=identifier,
                )
                return run.pk, created
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual(len({value[0] for value in results}), 1)
        self.assertEqual(sorted(value[1] for value in results), [False, True])
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(DBTaskResult.objects.count(), 1)

    def test_concurrent_submission_id_reuse_in_different_cases_is_independent(self):
        actor = get_user_model().objects.create_user(username="two-case-owner")
        originals = [
            TextEvidence.objects.create(
                case=Case.objects.create(owner=actor, name=f"Case {index}"),
                uploaded_by=actor,
                title=f"Synthetic source {index}",
                body="Synthetic retained text",
            )
            for index in range(2)
        ]
        submission_id = uuid4()
        both_checked_submission = Barrier(2)

        def resolve_after_both_checks(run, case, **kwargs):
            both_checked_submission.wait(timeout=10)
            return resolve_inputs(run, case, **kwargs)

        def submit(evidence):
            close_old_connections()
            try:
                _, created = start_run(
                    actor,
                    evidence.case_id,
                    "entities",
                    [{"kind": "text", "id": str(evidence.pk)}],
                    submission_id=submission_id,
                )
                return created
            finally:
                close_old_connections()

        with (
            patch(
                "core.analysis.source_resolution.resolve_inputs",
                side_effect=resolve_after_both_checks,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            self.assertEqual(list(executor.map(submit, originals)), [True, True])
        self.assertCountEqual(
            AnalysisRun.objects.values_list("case_id", flat=True),
            [original.case_id for original in originals],
        )
        self.assertEqual(DBTaskResult.objects.count(), 2)

    def test_real_worker_dispatches_once_and_retains_only_uuid_operational_result(self):
        user = get_user_model().objects.create_user(
            username="processing-queue-owner", email="queue@example.test"
        )
        EmailAddress.objects.create(
            user=user, email=user.email, verified=True, primary=True
        )
        case = Case.objects.create(owner=user, name="Synthetic processing queue")
        evidence = TextEvidence.objects.create(
            case=case, uploaded_by=user, title="Synthetic", body="queue@example.test"
        )
        run, _ = start_run(
            user, case.pk, "entities", [{"kind": "text", "id": str(evidence.pk)}]
        )
        queued = DBTaskResult.objects.get()
        drain_queue()
        queued.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(queued.status, "SUCCESSFUL")
        self.assertEqual(run.status, "complete")
        self.assertEqual(queued.return_value, str(run.result.pk))
        self.assertEqual(AnalysisResult.objects.filter(run=run).count(), 1)

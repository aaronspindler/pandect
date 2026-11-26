"""Private routes, case approval, pinned execution and legacy history."""

from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from core.analysis.approval import revoke_case_execution
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    available_executions,
    configured_executions,
    processing_external,
    requires_inference,
    snapshot_execution,
)
from core.analysis.services import start_run
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_gateway import CONFIG
from core.analysis.worker import process_run
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence
from geo.testing import LOCAL_GEOCODER
from text.models import TextEvidence


class ExecutionSelectionTests(SimpleTestCase):
    @override_settings(PANDECT_AI_GATEWAY={})
    def test_empty_gateway_configuration_does_not_enable_a_route(self):
        self.assertEqual(configured_executions("content_events", {}), {})
        self.assertEqual(configured_executions("transcription", {}), {})
        self.assertEqual(
            snapshot_execution("content_events", {"backend": "gateway"})["model"],
            "",
        )

    def test_gateway_methods_and_deterministic_options(self):
        for operation, parameters, purpose in (
            ("transcription", {}, "transcription"),
            ("content_events", {}, "text"),
            ("session_summaries", {}, "text"),
            ("text_topics", {}, "text"),
            ("text_topics", {"include_summary": False}, None),
            ("conversation_link", {"semantic_links": True}, "text"),
            ("conversation_link", {}, None),
            ("ocr", {}, "vision"),
        ):
            with self.subTest(operation=operation, parameters=parameters):
                self.assertEqual(requires_inference(operation, parameters), purpose)
                self.assertEqual(
                    processing_external(operation, parameters), purpose is not None
                )
                self.assertEqual(
                    processing_external(operation, parameters, "gateway"),
                    purpose is not None,
                )
        with self.assertRaises(ValidationError):
            processing_external("content_events", {}, "direct-provider")


class LocalExecutionTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic plan",
            body="The visit is cancelled. This is a reported statement.",
        )

    def start(self, operation="content_events", selections=None, **kwargs):
        return start_run(
            self.users[kwargs.pop("role", "owner")],
            self.case.pk,
            operation,
            selections or [{"kind": "text", "id": str(self.note.pk)}],
            **kwargs,
        )[0]

    def process(self, run):
        process_run(run.pk)
        run.refresh_from_db()
        return run.status

    def suggestion(self, run):
        return {
            "items": [
                {
                    "kind": "Reported cancellation",
                    "text": "The source reports a cancellation.",
                    "citations": [
                        {
                            "source_id": selected["id"],
                            "quote": "The visit is cancelled.",
                        }
                        for selected in run.inputs
                    ],
                }
            ]
        }

    def local_client(self, run):
        client = Mock()
        client.infer.return_value = self.suggestion(run)
        client.usage = {"request_count": 1, "input_tokens": 50, "output_tokens": 20}
        return client

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_cloud_requires_current_case_approval_for_the_exact_destination(self):
        parameters = {"backend": "gateway"}
        with self.assertRaisesMessage(
            AnalysisError, "external_processing_not_approved"
        ):
            self.start(
                parameters=parameters
                | {
                    "_external_approval": {"revision": str(uuid4())},
                    "external_processing": True,
                },
            )
        self.assertEqual(
            set(available_executions(self.case, "content_events", {})), set()
        )
        grant = external_grant_kwargs(self.case, "content_events")
        self.assertEqual(
            set(available_executions(self.case, "content_events", {})),
            {"gateway"},
        )
        with self.assertRaisesMessage(AnalysisError, "execution_changed"):
            start_run(
                self.users["outsider"],
                self.other_case.pk,
                "content_events",
                [{"kind": "text", "id": str(self.note.pk)}],
                parameters=parameters,
                **grant,
            )
        run = self.start(parameters=parameters, role="editor", **grant)
        self.assertTrue(run.external_processing)
        with (
            patch(
                "core.analysis.gateway.GatewayClient.infer",
                return_value=self.suggestion(run),
            ),
        ):
            self.assertEqual(self.process(run), "complete")
        self.assertEqual(
            run.result.provenance["external_approval"]["approved_by"],
            str(self.users["owner"].pk),
        )

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_case_revoke_and_reapprove_cannot_resume_previously_queued_work(self):
        parameters = {"backend": "gateway"}
        grant = external_grant_kwargs(self.case, "content_events")
        old = self.start(parameters=parameters, **grant)
        revoke_case_execution(self.case)
        new_grant = external_grant_kwargs(self.case, "content_events")
        self.assertNotEqual(
            grant["external_grant_revision"], new_grant["external_grant_revision"]
        )
        with patch("core.analysis.gateway.build_opener") as transport:
            self.assertEqual(self.process(old), "failed")
        transport.assert_not_called()
        self.assertEqual(old.error_code, "external_processing_not_approved")
        retry = self.start(parameters=parameters, previous_run=old, **new_grant)
        self.assertEqual(retry.previous_run_id, old.pk)

    def test_private_geographic_results_need_no_ai_or_external_approval(self):
        observation = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic place",
            address="1 Synthetic Street",
            latitude=43,
            longitude=-79,
        )
        selections = [{"kind": "location", "id": str(observation.pk)}]
        with (
            override_settings(
                PANDECT_GEOCODER_LOCAL=True,
                PANDECT_GEOCODER_URL="http://private-geocoder.test",
                PANDECT_GEOCODER_NAME="Synthetic geocoder",
                PANDECT_GEOCODER_VERSION="fixture-1",
                PANDECT_GEOCODER_ATTRIBUTION="Synthetic references",
                PANDECT_MAP_CONTEXT_LOCAL=True,
                PANDECT_MAP_CONTEXT_URL="http://private-map.test/api/interpreter",
                PANDECT_MAP_CONTEXT_NAME="Synthetic map",
                PANDECT_MAP_CONTEXT_VERSION="fixture-1",
                PANDECT_MAP_CONTEXT_ATTRIBUTION="Synthetic references",
            ),
            patch("core.analysis.gateway.GatewayClient.request") as cloud,
        ):
            run = self.start("geocode_locations", selections)
            with patch(
                "geo.analysis.external.geocoding.Geocoder.lookup", return_value=[]
            ):
                self.assertEqual(self.process(run), "complete", run.error_code)
            context = self.start("geographic_context", selections)
            with patch(
                "geo.analysis.external.geographic_context.ContextClient.lookup",
                return_value=b'{"elements":[],"osm3s":{"timestamp_osm_base":"2026-01-01T00:00:00Z"}}',
            ):
                self.assertEqual(self.process(context), "complete", context.error_code)
            self.assertFalse(run.external_processing)
            self.assertFalse(context.external_processing)
            self.assertEqual(context.result.artifacts.count(), 1)
        cloud.assert_not_called()

    @override_settings(**(LOCAL_GEOCODER | {"PANDECT_GEOCODER_LOCAL": False}))
    def test_revoked_grant_stops_external_geocoding_after_first_authorized_request(
        self,
    ):
        selections = []
        for number in (1, 2):
            observation = LocationEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["editor"],
                title=f"Synthetic place {number}",
                address=f"{number} Synthetic Street",
            )
            selections.append({"kind": "location", "id": str(observation.pk)})
        run = self.start(
            "geocode_locations",
            selections,
            role="editor",
            **external_grant_kwargs(self.case, "geocode_locations"),
        )

        def revoke(**kwargs):
            revoke_case_execution(self.case)
            return []

        with patch(
            "geo.analysis.external.geocoding.Geocoder.lookup", side_effect=revoke
        ) as lookup:
            self.assertEqual(self.process(run), "failed")
        lookup.assert_called_once()
        self.assertEqual(run.error_code, "external_processing_not_approved")
        self.assertFalse(hasattr(run, "result"))

    def test_legacy_queued_cloud_run_stops_and_keeps_its_route_and_parameters(self):
        selected = self.start("entities")
        legacy = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version="1",
            inputs=selected.inputs,
            external_processing=True,
            parameters={},
        )
        with (
            patch("core.analysis.gateway.GatewayClient.infer") as external,
        ):
            self.assertEqual(self.process(legacy), "failed")
        external.assert_not_called()
        legacy.refresh_from_db()
        self.assertEqual(legacy.parameters, {})
        self.assertTrue(legacy.external_processing)
        self.assertEqual(legacy.error_code, "execution_changed")

    @override_settings(PANDECT_GEOCODER_LOCAL=False, PANDECT_MAP_CONTEXT_LOCAL=False)
    def test_private_geographic_services_need_explicit_operator_trust(self):
        observation = LocationEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic address",
            address="1 Synthetic Street",
            latitude=43,
            longitude=-79,
        )
        selections = [{"kind": "location", "id": str(observation.pk)}]
        for operation, flag in (
            ("geocode_locations", "PANDECT_GEOCODER_LOCAL"),
            ("geographic_context", "PANDECT_MAP_CONTEXT_LOCAL"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesMessage(
                    AnalysisError, "external_processing_not_approved"
                ):
                    self.start(operation, selections)
                with override_settings(**{flag: True}):
                    run = self.start(operation, selections)
                    self.assertFalse(run.external_processing)
                    self.assertEqual(run.parameters["_execution"]["location"], "local")
                self.assertEqual(self.process(run), "failed")
                run.refresh_from_db()
                self.assertEqual(run.error_code, "execution_changed")

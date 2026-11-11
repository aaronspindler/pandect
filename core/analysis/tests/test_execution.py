"""New local routes, explicit disclosure, pinned execution and legacy history."""

import json
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from audio.models import AudioEvidence
from cases.models import CaseMembership
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import (
    processing_external,
    requires_inference,
    snapshot_execution,
)
from core.analysis.services import process_run, start_run
from core.analysis.testing import wav_bytes
from core.models import AnalysisRun, GatewayRequest
from core.testing import EvidenceViewTestMixin
from geo.models import LocationEvidence
from phone.models import TextMessageEvidence
from text.models import TextEvidence

LOCAL = {
    "text": {
        "url": "http://private-text.test:8080",
        "model": "synthetic-qwen",
        "sha256": "a" * 64,
        "runtime": "synthetic-llama-revision",
        "context_tokens": 32768,
    },
    "transcription": {
        "url": "http://private-audio.test:8080",
        "model": "synthetic-whisper",
        "sha256": "b" * 64,
        "runtime": "synthetic-whisper-revision",
    },
    "token": "never-retain-this-token",
    "timeout": 120,
    "max_request_bytes": 8388608,
    "max_response_bytes": 2097152,
    "max_output_tokens": 4096,
}


class ExecutionSelectionTests(SimpleTestCase):
    def test_all_five_ai_paths_and_optional_local_modes(self):
        for operation, parameters, purpose in (
            ("transcription", {}, "transcription"),
            ("content_events", {}, "text"),
            ("session_summaries", {}, "text"),
            ("text_topics", {}, "text"),
            ("text_topics", {"include_summary": False}, None),
            ("conversation_link", {"semantic_links": True}, "text"),
            ("conversation_link", {}, None),
            ("ocr", {}, None),
        ):
            with self.subTest(operation=operation, parameters=parameters):
                self.assertEqual(requires_inference(operation, parameters), purpose)
                self.assertFalse(processing_external(operation, parameters))
                self.assertEqual(
                    processing_external(operation, parameters, "gateway"),
                    purpose is not None,
                )
        with self.assertRaises(ValidationError):
            processing_external("content_events", {}, "direct-provider")

    @override_settings(PANDECT_LOCAL_AI=LOCAL)
    def test_snapshot_contains_artifact_and_bounds_but_no_secret(self):
        execution = snapshot_execution("content_events", {})
        self.assertEqual(execution["sha256"], "a" * 64)
        self.assertEqual(execution["context_tokens"], 32768)
        self.assertEqual(execution["adapter_version"], "2")
        self.assertEqual(execution["inference_settings"]["temperature"], 0)
        self.assertFalse(execution["inference_settings"]["enable_thinking"])
        self.assertNotIn("token", execution)
        self.assertNotIn(LOCAL["token"], str(execution))


@override_settings(PANDECT_LOCAL_AI=LOCAL)
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

    def test_new_runs_default_local_and_approval_never_selects_cloud(self):
        run = self.start(external_processing=True)
        self.assertFalse(run.external_processing)
        self.assertEqual(run.parameters["_execution"]["backend"], "local")
        with (
            patch(
                "core.analysis.local.LocalClient", return_value=self.local_client(run)
            ),
            patch("core.analysis.gateway.GatewayClient.request") as cloud,
        ):
            self.assertEqual(self.process(run), "complete")
        cloud.assert_not_called()
        self.assertEqual(GatewayRequest.objects.count(), 0)
        self.assertEqual(run.result.provenance["execution"]["sha256"], "a" * 64)
        self.assertEqual(run.result.provenance["local_usage"]["request_count"], 1)

    def test_cloud_requires_explicit_selection_and_approval(self):
        with self.assertRaisesMessage(ValidationError, "Approve external"):
            self.start(parameters={"backend": "gateway"})
        run = self.start(parameters={"backend": "gateway"}, external_processing=True)
        self.assertTrue(run.external_processing)
        with (
            patch(
                "core.analysis.gateway.GatewayClient.infer",
                return_value=self.suggestion(run),
            ),
            patch("core.analysis.local.LocalClient") as local,
        ):
            self.assertEqual(self.process(run), "complete")
        local.assert_not_called()

    def test_each_text_mode_runs_local_with_existing_source_validation(self):
        messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Message {index}",
                body="The visit is cancelled.",
                sender=self.person,
                recipient=self.person,
                occurred_at=timezone.now() + timedelta(minutes=index),
            )
            for index in range(2)
        ]
        selections = [{"kind": "text-message", "id": str(item.pk)} for item in messages]
        for operation, parameters in (
            ("content_events", {}),
            ("session_summaries", {}),
            ("text_topics", {"include_summary": True}),
            ("conversation_link", {"semantic_links": True}),
        ):
            with self.subTest(operation=operation):
                run = self.start(operation, selections, parameters=parameters)
                with (
                    patch(
                        "core.analysis.local.LocalClient",
                        return_value=self.local_client(run),
                    ),
                    patch("core.analysis.gateway.GatewayClient.request") as cloud,
                ):
                    self.assertEqual(self.process(run), "complete", run.error_code)
                cloud.assert_not_called()
                self.assertFalse(run.external_processing)
                self.assertEqual(run.result.provenance["execution"]["backend"], "local")

    def test_local_transcription_keeps_original_intervals_and_private_search(self):
        audio = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic PCM",
            file=ContentFile(wav_bytes(), name="synthetic.wav"),
        )
        run = self.start("transcription", [{"kind": "audio", "id": str(audio.pk)}])
        client = Mock()
        client.request.return_value = {
            "text": "Synthetic speech",
            "segments": [
                {"text": "Synthetic speech", "start": 0, "end": 1},
            ],
        }
        client.usage = {"request_count": 2, "input_tokens": None}
        with (
            patch("core.analysis.local.LocalClient", return_value=client),
            patch("core.analysis.gateway.GatewayClient.request") as cloud,
        ):
            self.assertEqual(self.process(run), "complete", run.error_code)
        cloud.assert_not_called()
        interval = run.result.data["items"][0]["anchors"][0]
        self.assertEqual((interval["start_seconds"], interval["end_seconds"]), (0, 1))
        self.assertEqual(run.result.passages.get().text, "Synthetic speech")
        self.assertEqual(
            run.result.data["items"][0]["values"]["Average log probability"],
            "Not supplied",
        )

    def test_revocation_during_transcription_stops_before_another_chunk(self):
        audio = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Synthetic multi-chunk recording",
            file=ContentFile(wav_bytes(seconds=31), name="synthetic.wav"),
        )
        run = self.start(
            "transcription", [{"kind": "audio", "id": str(audio.pk)}], role="editor"
        )

        def respond(request, *, timeout):
            if request.get_method() == "GET":
                identity = LOCAL["transcription"] | {"kind": "transcription"}
                return BytesIO(json.dumps(identity).encode())
            CaseMembership.objects.filter(
                case=self.case, user=self.users["editor"]
            ).delete()
            return BytesIO(b'{"text": "Synthetic first chunk"}')

        with patch("core.analysis.local.build_opener") as build:
            request = build.return_value.open
            request.side_effect = respond
            self.assertEqual(self.process(run), "failed")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(run.error_code, "access_removed")
        self.assertFalse(hasattr(run, "result"))

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

    def test_configuration_drift_refuses_work_and_preserves_submission_identity(self):
        submission_id = uuid4()
        run = self.start(submission_id=submission_id)
        changed = deepcopy(LOCAL)
        changed["text"]["sha256"] = "c" * 64
        with override_settings(PANDECT_LOCAL_AI=changed):
            self.assertEqual(self.start(submission_id=submission_id).pk, run.pk)
            with (
                patch("core.analysis.local.LocalClient") as local,
                patch("core.analysis.gateway.GatewayClient.request") as cloud,
            ):
                self.assertEqual(self.process(run), "failed")
            local.assert_not_called()
            cloud.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.error_code, "execution_changed")
        self.assertIn("settings changed", error_message(run.error_code))
        self.assertIn("start another run", error_message(run.error_code))
        self.assertEqual(run.parameters["_execution"]["sha256"], "a" * 64)

    def test_caller_cannot_supply_a_trusted_execution_snapshot(self):
        run = self.start(parameters={"_execution": {"backend": "gateway"}})
        self.assertEqual(run.parameters["_execution"]["backend"], "local")

    def test_local_runtime_failure_never_falls_back_or_publishes(self):
        run = self.start()
        with (
            patch(
                "core.analysis.local.LocalClient.infer",
                side_effect=AnalysisError("local_ai_unavailable"),
            ),
            patch("core.analysis.gateway.GatewayClient.request") as cloud,
        ):
            self.assertEqual(self.process(run), "failed")
        cloud.assert_not_called()
        self.assertFalse(hasattr(run, "result"))

    def test_revocation_after_local_inference_withholds_results(self):
        run = self.start(role="editor")

        def revoke(prompt, schema):
            CaseMembership.objects.filter(
                case=self.case, user=self.users["editor"]
            ).delete()
            return self.suggestion(run)

        with patch("core.analysis.local.LocalClient.infer", side_effect=revoke):
            self.assertEqual(self.process(run), "failed")
        run.refresh_from_db()
        self.assertEqual(run.error_code, "access_removed")
        self.assertFalse(hasattr(run, "result"))

    def test_legacy_queued_cloud_run_keeps_its_route_and_parameters(self):
        selected = self.start()
        legacy = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.users["owner"],
            operation="content_events",
            method_version=selected.method_version,
            inputs=selected.inputs,
            external_processing=True,
            parameters={},
        )
        with (
            patch(
                "core.analysis.gateway.GatewayClient.infer",
                return_value=self.suggestion(legacy),
            ),
            patch("core.analysis.local.LocalClient") as local,
        ):
            self.assertEqual(self.process(legacy), "complete")
        local.assert_not_called()
        legacy.refresh_from_db()
        self.assertEqual(legacy.parameters, {})
        self.assertTrue(legacy.external_processing)

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
                with self.assertRaisesMessage(ValidationError, "Approve external"):
                    self.start(operation, selections)
                with override_settings(**{flag: True}):
                    run = self.start(operation, selections)
                    self.assertFalse(run.external_processing)
                    self.assertEqual(run.parameters["_execution"]["location"], "local")
                self.assertEqual(self.process(run), "failed")
                run.refresh_from_db()
                self.assertEqual(run.error_code, "execution_changed")

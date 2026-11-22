"""OpenAI BYOK routing, retained execution and destination-bound consent."""

import json
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from django_tasks_db.models import DBTaskResult

from core.analysis.errors import AnalysisError
from core.analysis.execution import gateway_execution, snapshot_execution
from core.analysis.services import authorize_run, process_run, start_run
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_execution import LOCAL
from core.analysis.tests.test_gateway import CONFIG, error, response
from core.models import AnalysisRun, GatewayRequest
from core.testing import EvidenceViewTestMixin
from phone.models import TextMessageEvidence

OPENAI = CONFIG | {
    "text_provider": "openai",
    "openai_key_alias": "default",
    "workers_token": "",
    "models": {
        "text": "gpt-6.2-sol",
        "transcription": "@cf/openai/whisper-large-v3-turbo",
    },
}
AUTO_OPENAI = OPENAI | {"models": OPENAI["models"] | {"text": "auto"}}


def openai_reply(value=None):
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": json.dumps(value or {"items": []})}
                ],
            },
        ],
        "usage": {"input_tokens": 17, "output_tokens": 8},
    }


@override_settings(PANDECT_AI_GATEWAY=OPENAI)
class OpenAIGatewayTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        from core.analysis.gateway import GatewayClient

        self.note = self.text()
        self.run, _ = start_run(
            self.users["owner"],
            self.case.pk,
            "content_events",
            [{"kind": "text", "id": str(self.note.pk)}],
            parameters={"backend": "gateway"},
            **external_grant_kwargs(self.case, "content_events"),
        )
        self.authorize = Mock(side_effect=lambda **kw: authorize_run(self.run, **kw))
        self.gateway = GatewayClient(self.run, self.authorize)

    def test_stored_key_uses_only_gateway_auth_and_responses_contract(self):
        with patch("core.analysis.gateway.build_opener") as build:
            build.return_value.open.return_value = response(openai_reply())
            self.assertEqual(
                self.gateway.infer("Synthetic evidence", {"type": "object"}),
                {"items": []},
            )
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            f"https://gateway.ai.cloudflare.com/v1/{'a' * 32}/"
            "synthetic-investigation/openai/responses",
        )
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(
            headers["cf-aig-authorization"], "Bearer synthetic-gateway-secret"
        )
        self.assertEqual(headers["cf-aig-byok-alias"], "default")
        self.assertEqual(headers["cf-aig-request-timeout"], "300000")
        self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 300)
        self.assertNotIn("authorization", headers)
        self.assertEqual(headers["user-agent"], "Pandect/0.1")
        self.assertEqual(headers["cf-aig-collect-log"], "false")
        self.assertEqual(headers["cf-aig-skip-cache"], "true")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "gpt-6.2-sol")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["max_output_tokens"], 32768)
        self.assertEqual(payload["text"]["format"], {"type": "json_object"})
        self.assertFalse(payload["store"])
        self.assertNotIn("temperature", payload)
        self.assertIn("untrusted", payload["input"][0]["content"])
        usage = GatewayRequest.objects.get()
        self.assertEqual((usage.input_tokens, usage.output_tokens), (17, 8))
        self.assertNotIn("secret", json.dumps(self.run.parameters))

    def test_transcription_retains_workers_route_and_requires_workers_credential(self):
        route = snapshot_execution("transcription", {"backend": "gateway"})
        self.assertEqual(route["provider"], "workers-ai")
        self.assertNotIn("key_alias", route)
        self.assertEqual(route["timeout"], CONFIG["timeout"])
        self.assertNotIn("inference_settings", route)
        with patch("core.analysis.gateway.build_opener") as build:
            with self.assertRaisesMessage(AnalysisError, "gateway_not_configured"):
                self.gateway.request("transcription", {"audio": []})
        build.assert_not_called()

    def test_refused_incomplete_malformed_and_tool_output_fail_without_a_result(self):
        cases = [
            (openai_reply() | {"status": "incomplete"}, "gateway_incomplete_response"),
            (
                openai_reply() | {"output": [{"type": "function_call"}]},
                "gateway_invalid_response",
            ),
            (openai_reply() | {"output": None}, "gateway_invalid_response"),
        ]
        refused = openai_reply()
        refused["output"][1]["content"] = [
            {"type": "refusal", "refusal": "Private provider detail"}
        ]
        cases.append((refused, "gateway_refused"))
        for text in ("not JSON", "[]", '{"items": NaN}'):
            invalid = openai_reply()
            invalid["output"][1]["content"][0]["text"] = text
            cases.append((invalid, "gateway_invalid_response"))
        for reply, code in cases:
            with (
                self.subTest(code=code),
                patch("core.analysis.gateway.build_opener") as build,
            ):
                build.return_value.open.return_value = response(reply)
                with self.assertRaisesMessage(AnalysisError, code):
                    self.gateway.infer("Synthetic", {})
        self.assertFalse(hasattr(self.run, "result"))

    def test_configuration_and_alias_drift_block_dispatch_and_retry(self):
        for updates in (
            {"text_provider": "workers-ai"},
            {"models": OPENAI["models"] | {"text": "gpt-6-astra"}},
            {"openai_key_alias": "another-key"},
        ):
            with (
                self.subTest(updates=updates),
                override_settings(PANDECT_AI_GATEWAY=OPENAI | updates),
                patch("core.analysis.gateway.build_opener") as build,
            ):
                with self.assertRaises(AnalysisError):
                    self.gateway.infer("Synthetic", {})
                build.assert_not_called()

        def change_settings(*args):
            self.settings_override = self.settings(
                PANDECT_AI_GATEWAY=OPENAI | {"openai_key_alias": "changed"}
            )
            self.settings_override.enable()
            self.addCleanup(self.settings_override.disable)

        with (
            patch("core.analysis.gateway.build_opener") as build,
            patch("core.analysis.gateway.time.sleep", side_effect=change_settings),
        ):
            build.return_value.open.side_effect = error(429)
            with self.assertRaisesMessage(AnalysisError, "execution_changed"):
                self.gateway.infer("Synthetic", {})
            self.assertEqual(build.return_value.open.call_count, 1)

    def test_legacy_cloud_jobs_require_new_approval_before_using_openai(self):
        self.run.parameters = {"backend": "gateway"}
        AnalysisRun.objects.filter(pk=self.run.pk).update(
            parameters=self.run.parameters
        )
        with patch("core.analysis.gateway.build_opener") as build:
            with self.assertRaisesMessage(AnalysisError, "execution_changed"):
                self.gateway.infer("Synthetic", {})
        build.assert_not_called()

    def test_switch_to_auto_preserves_approval_only_for_the_same_model(self):
        parameters = {"backend": "gateway"}
        synthesis = snapshot_execution("question_synthesis", parameters)
        with (
            override_settings(PANDECT_AI_GATEWAY=AUTO_OPENAI),
            patch("core.analysis.gateway.build_opener") as build,
        ):
            self.assertEqual(
                snapshot_execution("question_synthesis", parameters), synthesis
            )
            with self.assertRaisesMessage(AnalysisError, "execution_changed"):
                self.gateway.infer("Synthetic", {})
        build.assert_not_called()
        self.assertFalse(GatewayRequest.objects.exists())

    def test_old_reasoning_settings_require_a_deliberate_rerun(self):
        execution = self.run.parameters["_execution"]
        execution["adapter_version"] = "gateway-3"
        execution["timeout"] = OPENAI["timeout"]
        execution["inference_settings"]["reasoning"]["effort"] = "low"
        execution["inference_settings"]["max_output_tokens"] = 8192
        AnalysisRun.objects.filter(pk=self.run.pk).update(
            parameters=self.run.parameters
        )
        with patch("core.analysis.gateway.build_opener") as build:
            process_run(self.run.pk)
        build.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "failed")
        self.assertEqual(self.run.error_code, "execution_changed")
        self.assertFalse(hasattr(self.run, "result"))
        self.assertFalse(GatewayRequest.objects.exists())

    def test_exhausted_reasoning_budget_and_timeout_publish_no_partial_result(self):
        for failure, expected in (
            (
                response(
                    openai_reply()
                    | {
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                    }
                ),
                "gateway_incomplete_response",
            ),
            (TimeoutError("Synthetic provider timeout"), "gateway_unavailable"),
        ):
            with (
                self.subTest(expected=expected),
                patch("core.analysis.gateway.build_opener") as build,
            ):
                # A fresh deliberate run is required after a failed attempt.
                run, _ = start_run(
                    self.users["owner"],
                    self.case.pk,
                    "content_events",
                    [{"kind": "text", "id": str(self.note.pk)}],
                    parameters={"backend": "gateway"},
                    **external_grant_kwargs(self.case, "content_events"),
                )
                build.return_value.open.side_effect = (
                    failure if isinstance(failure, Exception) else [failure]
                )
                process_run(run.pk)
                run.refresh_from_db()
                self.assertEqual(run.status, "failed")
                self.assertEqual(run.error_code, expected)
                self.assertFalse(hasattr(run, "result"))
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_transport_retry_keeps_the_approved_reasoning_and_limits(self):
        with (
            patch("core.analysis.gateway.build_opener") as build,
            patch("core.analysis.gateway.time.sleep"),
        ):
            build.return_value.open.side_effect = [error(429), response(openai_reply())]
            process_run(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "empty", self.run.error_code)
        calls = build.return_value.open.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[0].data, calls[1].args[0].data)
        for call in calls:
            payload = json.loads(call.args[0].data)
            self.assertEqual(payload["reasoning"], {"effort": "high"})
            self.assertEqual(payload["max_output_tokens"], 32768)
            self.assertEqual(call.kwargs["timeout"], 300)
        self.assertEqual(
            list(
                self.run.gateway_requests.order_by("created_at").values_list(
                    "status", flat=True
                )
            ),
            ["rate_limit", "complete"],
        )

    def test_jobs_approved_with_the_removed_daily_cap_require_fresh_review(self):
        self.assertNotIn("daily_requests", self.run.parameters["_execution"])
        self.run.parameters["_execution"]["daily_requests"] = 100
        AnalysisRun.objects.filter(pk=self.run.pk).update(
            parameters=self.run.parameters
        )
        with patch("core.analysis.gateway.build_opener") as build:
            with self.assertRaisesMessage(AnalysisError, "execution_changed"):
                self.gateway.infer("Synthetic", {})
        build.assert_not_called()
        self.assertFalse(GatewayRequest.objects.exists())

    def test_missing_approval_and_revoked_access_make_no_request(self):
        self.run.external_processing = False
        with patch("core.analysis.gateway.build_opener") as build:
            with self.assertRaisesMessage(
                AnalysisError, "external_processing_not_approved"
            ):
                self.gateway.infer("Synthetic", {})
        build.assert_not_called()
        self.run.external_processing = True
        self.authorize.side_effect = PermissionDenied
        with patch("core.analysis.gateway.build_opener") as build:
            with self.assertRaises(PermissionDenied):
                self.gateway.infer("Synthetic", {})
        build.assert_not_called()

    def test_completed_analysis_publishes_validated_citations_and_provider_provenance(
        self,
    ):
        value = {
            "items": [
                {
                    "kind": "reported",
                    "text": "A source reports wording.",
                    "citations": [
                        {"source_id": self.run.inputs[0]["id"], "quote": self.note.body}
                    ],
                }
            ]
        }
        with patch("core.analysis.gateway.build_opener") as build:
            build.return_value.open.return_value = response(openai_reply(value))
            process_run(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "complete", self.run.error_code)
        retained = self.run.result.provenance["execution"]
        self.assertEqual(retained["provider"], "openai")
        self.assertEqual(retained["model"], "gpt-6.2-sol")
        self.assertNotIn("gateway_token", retained)

    @override_settings(PANDECT_AI_GATEWAY=AUTO_OPENAI)
    def test_all_external_text_methods_use_their_approved_openai_models(self):
        messages = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                sender=self.person,
                recipient=self.person,
                title=f"Synthetic message {index}",
                body="The proposed meeting remains unconfirmed.",
                occurred_at=datetime(2026, 11, 16, 12, index, tzinfo=UTC),
            )
            for index in range(2)
        ]
        for operation, sources, parameters, model, effort in (
            ("content_events", [("text", self.note)], {}, "gpt-6-luna", "high"),
            (
                "text_topics",
                [("text", self.note)],
                {"include_summary": True},
                "gpt-6-luna",
                "medium",
            ),
            (
                "session_summaries",
                [("text-message", row) for row in messages],
                {},
                "gpt-6.1-terra",
                "medium",
            ),
            (
                "conversation_link",
                [("text-message", row) for row in messages],
                {"semantic_links": True},
                "gpt-6.1-terra",
                "high",
            ),
            (
                "question_synthesis",
                [("text", self.note)],
                {
                    "question": "What is reported?",
                    "text_source_ids": [self.run.inputs[0]["id"]],
                    "finding_refs": [],
                },
                "gpt-6.2-sol",
                "high",
            ),
        ):
            with self.subTest(operation=operation):
                run, _ = start_run(
                    self.users["owner"],
                    self.case.pk,
                    operation,
                    [{"kind": kind, "id": str(item.pk)} for kind, item in sources],
                    parameters=parameters | {"backend": "gateway"},
                    **external_grant_kwargs(self.case, operation),
                )
                value = {"items": []}
                if operation == "question_synthesis":
                    value["unknowns"] = ["The sources do not establish an answer."]
                with patch("core.analysis.gateway.build_opener") as build:
                    build.return_value.open.return_value = response(openai_reply(value))
                    process_run(run.pk)
                    self.assertTrue(build.return_value.open.called)
                    for call in build.return_value.open.call_args_list:
                        payload = json.loads(call.args[0].data)
                        self.assertEqual(payload["model"], model)
                        self.assertEqual(payload["reasoning"], {"effort": effort})
                        self.assertEqual(payload["max_output_tokens"], 32768)
                        self.assertEqual(call.kwargs["timeout"], 300)
                run.refresh_from_db()
                self.assertIn(
                    run.status, {"complete", "empty", "partial"}, run.error_code
                )
                self.assertEqual(run.parameters["_execution"]["model"], model)
                self.assertEqual(
                    set(run.gateway_requests.values_list("model", flat=True)), {model}
                )
                self.assertEqual(
                    run.result.provenance["execution"]["provider"], "openai"
                )
                self.assertEqual(
                    run.result.provenance["execution"], run.parameters["_execution"]
                )
                self.assertEqual(
                    run.result.provenance["execution"]["inference_settings"],
                    {
                        "store": False,
                        "reasoning": {"effort": effort},
                        "max_output_tokens": 32768,
                        "text": {"format": {"type": "json_object"}},
                    },
                )

    def test_invalid_source_quotation_publishes_no_result(self):
        value = {
            "items": [
                {
                    "kind": "reported",
                    "text": "Unsupported claim",
                    "citations": [
                        {
                            "source_id": self.run.inputs[0]["id"],
                            "quote": "Words absent from the original source.",
                        }
                    ],
                }
            ]
        }
        with patch("core.analysis.gateway.build_opener") as build:
            build.return_value.open.return_value = response(openai_reply(value))
            process_run(self.run.pk)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "failed")
        self.assertFalse(hasattr(self.run, "result"))


@override_settings(PANDECT_AI_GATEWAY=OPENAI)
class OpenAIApprovalTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = self.text()
        self.login("owner")
        self.analysis_url = reverse("core:analysis:create", args=[self.case.pk])

    def payload(self):
        page = self.client.get(self.analysis_url, {"operation": "content_events"})
        self.assertNotContains(page, 'name="backend"')
        self.assertNotContains(page, 'name="external_processing"')
        return {
            "submission_id": page.context["form"]["submission_id"].value(),
            "operation": "content_events",
            "sources": [f"text:{self.note.pk}"],
        }

    @override_settings(PANDECT_LOCAL_AI={})
    def test_owner_approval_allows_editor_to_create_run_for_current_destination(self):
        payload = self.payload()
        self.login("editor")
        denied = self.client.post(
            self.analysis_url, payload | {"external_processing": "on"}
        )
        self.assertContains(denied, "No configured or approved service")
        self.assertEqual(denied.context["form"]["sources"].value(), payload["sources"])
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())

        external_grant_kwargs(self.case, "content_events")
        self.case.ai_backend = "gateway"
        self.case.save(update_fields=["ai_backend"])
        saved = self.client.post(self.analysis_url, payload)
        self.assertEqual(saved.status_code, 302)
        run = AnalysisRun.objects.get()
        self.assertEqual(run.requested_by, self.users["editor"])
        self.assertTrue(run.external_processing)
        self.assertEqual(run.parameters["_execution"]["model"], "gpt-6.2-sol")

    @override_settings(PANDECT_LOCAL_AI=LOCAL)
    def test_changed_destination_skips_external_and_keeps_private_analysis(self):
        external_grant_kwargs(self.case, "content_events")
        payload = self.payload()
        with self.settings(PANDECT_AI_GATEWAY=OPENAI | {"openai_key_alias": "changed"}):
            browsed = self.client.post(
                self.analysis_url,
                payload | {"browse_sources": "search", "q": "Synthetic"},
            )
            self.assertEqual(
                browsed.context["form"]["sources"].value(), payload["sources"]
            )
            result = self.client.post(self.analysis_url, payload)
        self.assertEqual(result.status_code, 302)
        run = AnalysisRun.objects.get()
        self.assertFalse(run.external_processing)
        self.assertEqual(run.parameters["_execution"]["backend"], "local")

    def test_snapshots_and_disclosures_include_alias_but_no_credentials(self):
        retained = gateway_execution("text", "content_events")
        self.assertEqual(retained["key_alias"], "default")
        self.assertNotIn("synthetic-gateway-secret", json.dumps(retained))

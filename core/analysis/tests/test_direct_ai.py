"""Direct providers use reviewed destinations and the existing usage ledger."""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.http import NoRedirect
from core.analysis.services import start_run
from core.analysis.testing import external_grant_kwargs
from core.analysis.tests.test_gateway import TRANSCRIPTION_CONFIG, error, response
from core.analysis.tests.test_openai_gateway import openai_reply
from core.analysis.worker import process_run
from core.models import AnalysisRun, GatewayRequest
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence

DIRECT_AI = {
    "openai_api_key": "synthetic-openai-secret",
    "anthropic_api_key": "synthetic-anthropic-secret",
    "openai_model": "gpt-6-luna",
    "anthropic_model": "claude-haiku-5-5",
}
OPENAI_DIRECT = TRANSCRIPTION_CONFIG | {
    "text_provider": "openai-direct",
    "account": "",
    "gateway": "",
    "gateway_token": "",
    "workers_token": "",
}
ANTHROPIC_DIRECT = OPENAI_DIRECT | {"text_provider": "anthropic-direct"}


def anthropic_reply(text='{"items": []}'):
    return {
        "type": "message",
        "role": "assistant",
        "stop_reason": "end_turn",
        "content": [
            {
                "type": "thinking",
                "thinking": "Synthetic provider reasoning",
                "signature": "synthetic-signature",
            },
            {"type": "redacted_thinking", "data": "synthetic-redacted-reasoning"},
            {"type": "text", "text": text},
        ],
        "usage": {"input_tokens": 19, "output_tokens": 7},
    }


@override_settings(PANDECT_AI_GATEWAY=OPENAI_DIRECT, PANDECT_DIRECT_AI=DIRECT_AI)
class DirectAITests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.note = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic source",
            body="A synthetic evidence passage.",
        )

    def queue(self, role="owner"):
        run, _ = start_run(
            self.users[role],
            self.case.pk,
            "content_events",
            [{"kind": "text", "id": str(self.note.pk)}],
            parameters={"backend": "gateway"},
            **external_grant_kwargs(self.case, "content_events"),
        )
        return run

    def assert_usage_and_provenance(self, run, request, model, provider, tokens):
        run.refresh_from_db()
        self.assertEqual(run.status, AnalysisRun.Status.EMPTY, run.error_code)
        execution = run.result.provenance["execution"]
        self.assertEqual(execution, run.parameters["_execution"])
        self.assertEqual(execution["provider"], provider)
        self.assertEqual(execution["model"], model)
        usage = GatewayRequest.objects.get(run=run, case=self.case)
        self.assertEqual(usage.model, model)
        self.assertEqual(usage.status, "complete")
        self.assertEqual(usage.request_bytes, len(request.data))
        self.assertEqual((usage.input_tokens, usage.output_tokens), tokens)
        retained = json.dumps(run.result.provenance)
        for key in ("openai_api_key", "anthropic_api_key"):
            self.assertNotIn(DIRECT_AI[key], retained)
        self.assertNotIn(self.note.body, retained)

    def test_openai_uses_responses_with_storage_disabled(self):
        run = self.queue()
        with patch("core.analysis.gateway.build_opener") as build:
            build.return_value.open.return_value = response(openai_reply())
            process_run(run.pk)
        request = build.return_value.open.call_args.args[0]
        self.assertIsInstance(build.call_args.args[0], NoRedirect)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(
            headers["authorization"], f"Bearer {DIRECT_AI['openai_api_key']}"
        )
        self.assertNotIn("cf-aig-authorization", headers)
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], DIRECT_AI["openai_model"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"], {"type": "json_object"})
        self.assertEqual(payload["input"][0]["role"], "system")
        self.assertIn("untrusted", payload["input"][0]["content"])
        self.assertIn(self.note.body, payload["input"][1]["content"])
        self.assert_usage_and_provenance(
            run, request, DIRECT_AI["openai_model"], "openai", (17, 8)
        )

    @override_settings(PANDECT_AI_GATEWAY=ANTHROPIC_DIRECT)
    def test_anthropic_uses_messages_and_separate_system_instructions(self):
        self.case.external_analysis_efforts = {"content_events": "xhigh"}
        self.case.save(update_fields=["external_analysis_efforts"])
        run = self.queue()
        with patch("core.analysis.gateway.build_opener") as build:
            build.return_value.open.return_value = response(anthropic_reply())
            process_run(run.pk)
        request = build.return_value.open.call_args.args[0]
        self.assertIsInstance(build.call_args.args[0], NoRedirect)
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-api-key"], DIRECT_AI["anthropic_api_key"])
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cf-aig-authorization", headers)
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], DIRECT_AI["anthropic_model"])
        self.assertEqual(payload["output_config"], {"effort": "xhigh"})
        self.assertNotIn("reasoning", payload)
        self.assertGreater(payload["max_tokens"], 0)
        self.assertIn("untrusted", payload["system"])
        self.assertEqual([message["role"] for message in payload["messages"]], ["user"])
        self.assertIn(self.note.body, payload["messages"][0]["content"])
        self.assert_usage_and_provenance(
            run, request, DIRECT_AI["anthropic_model"], "anthropic", (19, 7)
        )
        self.assertEqual(
            run.result.provenance["execution"]["inference_settings"]["output_config"],
            payload["output_config"],
        )
        self.assertNotIn("Synthetic provider reasoning", json.dumps(run.result.data))

    @override_settings(PANDECT_AI_GATEWAY=ANTHROPIC_DIRECT)
    def test_incomplete_refused_and_malformed_messages_publish_no_result(self):
        for reply, code in (
            (
                anthropic_reply() | {"stop_reason": "max_tokens"},
                "gateway_incomplete_response",
            ),
            (
                anthropic_reply() | {"stop_reason": "refusal"},
                "gateway_refused",
            ),
            (
                anthropic_reply() | {"content": [{"type": "tool_use", "input": {}}]},
                "gateway_invalid_response",
            ),
            (anthropic_reply('{"items": NaN}'), "gateway_invalid_response"),
        ):
            with (
                self.subTest(code=code),
                patch("core.analysis.gateway.build_opener") as build,
            ):
                run = self.queue()
                build.return_value.open.return_value = response(reply)
                process_run(run.pk)
                run.refresh_from_db()
                self.assertEqual(run.status, AnalysisRun.Status.FAILED)
                self.assertEqual(run.error_code, code)
                self.assertFalse(hasattr(run, "result"))

    @override_settings(
        PANDECT_AI_GATEWAY=TRANSCRIPTION_CONFIG | {"text_provider": "openai-direct"}
    )
    def test_provider_failure_retries_only_the_approved_endpoint(self):
        run = self.queue()
        with (
            patch("core.analysis.gateway.build_opener") as build,
            patch("core.analysis.gateway.time.sleep"),
        ):
            build.return_value.open.side_effect = [error(429), error(503)]
            process_run(run.pk)
        urls = [
            call.args[0].full_url for call in build.return_value.open.call_args_list
        ]
        self.assertEqual(urls, ["https://api.openai.com/v1/responses"] * 2)
        self.assertEqual(
            list(
                run.gateway_requests.order_by("created_at").values_list(
                    "status", flat=True
                )
            ),
            ["rate_limit", "provider_failure"],
        )
        run.refresh_from_db()
        self.assertEqual(run.error_code, "gateway_provider_failure")
        self.assertFalse(hasattr(run, "result"))

    def test_changed_destination_and_revoked_access_block_queued_requests(self):
        changed = self.queue()
        revoked = self.queue("editor")
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        with patch("core.analysis.gateway.build_opener") as build:
            with self.settings(PANDECT_AI_GATEWAY=ANTHROPIC_DIRECT):
                process_run(changed.pk)
            process_run(revoked.pk)
        build.assert_not_called()
        changed.refresh_from_db()
        revoked.refresh_from_db()
        self.assertEqual(changed.error_code, "execution_changed")
        self.assertEqual(revoked.error_code, "access_removed")
        self.assertFalse(GatewayRequest.objects.exists())

    @override_settings(PANDECT_LOCAL_AI={})
    def test_analysis_requires_renewed_approval_after_switching_direct_providers(self):
        self.login("owner")
        url = reverse("core:analysis:create", args=[self.case.pk])
        settings_url = reverse("cases:case_settings", args=[self.case.pk])
        action = reverse("cases:case_ai_settings", args=[self.case.pk])

        def approve_current():
            page = self.client.get(settings_url)
            form = page.context["ai_settings_form"]
            draft = {
                "confirmation": form.expected_confirmation,
                "ai_backend": "gateway",
            }
            if form.initial["ai_backend"] == "gateway":
                draft = {"action": "review_current"}
            review = self.client.post(action, draft)
            self.assertNotContains(review, DIRECT_AI["openai_api_key"])
            self.assertNotContains(review, DIRECT_AI["anthropic_api_key"])
            self.assertEqual(
                self.client.post(
                    action,
                    dict(review.context["proposal"])
                    | {
                        "action": "approve",
                        "allow": [
                            row["scope_id"] for row in review.context["destinations"]
                        ],
                        "review_confirmation": review.context["review_confirmation"],
                    },
                ).status_code,
                302,
            )

        approve_current()
        form = self.client.get(url, {"operation": "content_events"}).context["form"]
        self.assertNotIn("backend", form.fields)
        data = {
            "operation": "content_events",
            "sources": [f"text:{self.note.pk}"],
            "submission_id": form["submission_id"].value(),
        }
        with self.settings(PANDECT_AI_GATEWAY=ANTHROPIC_DIRECT):
            rejected = self.client.post(url, data)
            self.assertNotEqual(rejected.status_code, 302)
            self.assertFalse(AnalysisRun.objects.exists())
            approve_current()
            self.assertEqual(self.client.post(url, data).status_code, 302)
        reviewed = AnalysisRun.objects.get().parameters["_execution"]
        self.assertEqual(reviewed["model"], DIRECT_AI["anthropic_model"])

    @override_settings(
        PANDECT_DIRECT_AI=DIRECT_AI | {"openai_api_key": ""}, PANDECT_LOCAL_AI={}
    )
    def test_incomplete_external_configuration_is_not_offered(self):
        self.login("owner")
        detail = self.client.get(
            reverse("core:analysis:create", args=[self.case.pk]),
            {"operation": "content_events"},
        )
        self.assertNotContains(detail, f"OpenAI · {DIRECT_AI['openai_model']}")
        self.assertNotContains(detail, "I approve external processing")
        self.assertContains(detail, 'type="submit" disabled>Start analysis')
        bulk = self.client.get(reverse("core:analysis:reprocess", args=[self.case.pk]))
        self.assertNotContains(bulk, f"OpenAI · {DIRECT_AI['openai_model']}")
        self.assertNotContains(bulk, "Processing location")

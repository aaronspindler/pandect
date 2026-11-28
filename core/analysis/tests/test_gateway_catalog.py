"""Release model eligibility, exact routes and gateway wire contracts."""

import json
from base64 import b64encode
from functools import partial
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from audio.models import AudioEvidence
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    external_model_options,
    gateway_execution,
    pin_model_choice,
)
from core.analysis.gateway import (
    GatewayClient,
    endpoint_and_headers,
    gateway_route_configured,
)
from core.analysis.services import start_run
from core.analysis.tests.test_gateway import CONFIG, response
from core.analysis.tests.test_openai_gateway import openai_reply
from core.analysis.tests.testing import external_grant_kwargs, wav_bytes
from core.analysis.worker import authorize_run
from core.models import GatewayRequest
from core.tests.testing import EvidenceViewTestMixin

CREDITS = CONFIG | {"unified_billing": True}


def chat_reply(**choice):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"items":[]}'},
                **choice,
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }


@override_settings(PANDECT_AI_GATEWAY=CREDITS)
class GatewayCatalogTests(EvidenceViewTestMixin, TestCase):
    def route(self, model, operation="content_events"):
        self.case.external_analysis_models = {operation: model}
        purpose = "transcription" if operation == "transcription" else "text"
        return gateway_execution(purpose, operation, case=self.case)

    def test_each_catalog_text_protocol_dispatches_to_one_known_recipient(self):
        for model, recipient, endpoint, reply, param in [
            (
                "google/gemini-4-carbon",
                "google-vertex-ai",
                "chat/completions",
                chat_reply(),
                "messages",
            ),
            (
                "anthropic/claude-fable-5.2",
                "anthropic",
                "messages",
                {
                    "type": "message",
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": '{"items":[]}'}],
                },
                "system",
            ),
            ("openai/gpt-6.2-sol", "openai", "responses", openai_reply(), "input"),
            (
                "@cf/zai-org/glm-5.3",
                "workers-ai",
                "chat/completions",
                chat_reply(),
                "messages",
            ),
        ]:
            with self.subTest(model=model):
                self.case.external_analysis_efforts = {"content_events": "xhigh"}
                self.route(model)
                self.case.save(
                    update_fields=[
                        "external_analysis_models",
                        "external_analysis_efforts",
                    ]
                )
                note = self.text()
                run, _ = start_run(
                    self.users["owner"],
                    self.case.pk,
                    "content_events",
                    [{"kind": "text", "id": str(note.pk)}],
                    parameters={"backend": "gateway"},
                    **external_grant_kwargs(self.case, "content_events"),
                )
                with patch("core.analysis.gateway.build_opener") as build:
                    build.return_value.open.return_value = response(reply)
                    client = GatewayClient(run, partial(authorize_run, run))
                    self.assertEqual(
                        client.infer("Synthetic evidence", {}), {"items": []}
                    )
                    request = build.return_value.open.call_args.args[0]
                payload = json.loads(request.data)
                self.assertIn(param, payload)
                self.assertEqual(payload["model"], model)
                if endpoint == "messages":
                    self.assertEqual(payload["output_config"], {"effort": "xhigh"})
                    self.assertNotIn("reasoning", payload)
                elif endpoint == "responses":
                    self.assertEqual(payload["reasoning"], {"effort": "xhigh"})
                else:
                    self.assertNotIn("output_config", payload)
                    self.assertNotIn("reasoning", payload)
                headers = {k.lower(): v for k, v in request.header_items()}
                self.assertEqual(headers["cf-aig-allowed-providers"], recipient)
                self.assertEqual(headers["cf-aig-gateway-id"], CONFIG["gateway"])
                self.assertEqual(headers["cf-aig-collect-log"], "false")
                self.assertEqual(
                    headers["authorization"], f"Bearer {CONFIG['workers_token']}"
                )
                self.assertEqual(
                    request.full_url,
                    f"https://api.cloudflare.com/client/v4/accounts/{CONFIG['account']}/ai/v1/{endpoint}",
                )
                usage = GatewayRequest.objects.get(run=run)
                self.assertEqual(usage.model, model)
                if endpoint == "chat/completions":
                    self.assertEqual((usage.input_tokens, usage.output_tokens), (11, 3))

    def test_billing_requires_opt_in_and_stored_key_routes_block_fallback(self):
        with override_settings(PANDECT_AI_GATEWAY=CONFIG):
            choices = {o["value"]: o for o in external_model_options("content_events")}
            self.assertFalse(choices["anthropic/claude-sonnet-5.5"]["available"])
            self.assertNotIn("@cf/zai-org/glm-5.3", choices)
            self.assertTrue(
                gateway_route_configured("text", self.route("@cf/zai-org/glm-5.3"))
            )
        with override_settings(
            PANDECT_AI_GATEWAY=CONFIG | {"stored_key_providers": ("google-vertex-ai",)}
        ):
            route = self.route("google/gemini-4-carbon")
            self.assertTrue(gateway_route_configured("text", route))
            self.assertEqual(route["key_alias"], "default")
            self.assertEqual(
                endpoint_and_headers(route)[1]["cf-aig-no-wholesale"], "true"
            )
        self.assertFalse(gateway_route_configured("text", route))

    def test_picker_limits_text_choices_to_openai_and_anthropic(self):
        choices = {
            row["value"]: row["group"]
            for row in external_model_options("content_events")
        }
        self.assertEqual(set(choices.values()), {"OpenAI", "Anthropic"})
        self.assertNotIn("google/gemini-4-carbon", choices)
        self.assertEqual(choices["anthropic/claude-sonnet-5.5"], "Anthropic")
        self.assertEqual(choices["openai/gpt-6.2-sol"], "OpenAI")
        self.assertNotIn(CONFIG["models"]["text"], choices)
        self.assertNotIn("openai/gpt-transcribe", choices)

    def test_refusals_and_incomplete_chat_choices_never_become_answers(self):
        for choice, code in [
            ({"finish_reason": "length"}, "gateway_incomplete_response"),
            ({"finish_reason": "content_filter"}, "gateway_refused"),
            (
                {"message": {"role": "user", "content": "untrusted"}},
                "gateway_invalid_response",
            ),
            (
                {"message": {"role": "assistant", "refusal": "declined"}},
                "gateway_refused",
            ),
        ]:
            with (
                self.subTest(choice=choice),
                self.assertRaisesMessage(AnalysisError, code),
            ):
                GatewayClient._chat_text(chat_reply(**choice))

    def test_model_pins_cannot_move_from_direct_to_gateway_on_operator_change(self):
        gateway = CONFIG | {
            "text_provider": "openai",
            "models": {"text": "gpt-6.2-sol"},
        }
        with override_settings(PANDECT_AI_GATEWAY=gateway):
            pin = pin_model_choice("content_events", "gpt-6.2-sol")
            self.assertEqual(self.route(pin)["provider"], "openai")
        with override_settings(
            PANDECT_AI_GATEWAY=gateway | {"text_provider": "openai-direct"},
            PANDECT_DIRECT_AI={
                "openai_model": "gpt-6.2-sol",
                "openai_api_key": "synthetic",
            },
        ):
            self.assertEqual(self.route(pin)["model"], "")
            self.case.external_analysis_approval = {
                "executions": {
                    "content_events": {"provider": "openai", "model": "gpt-6.2-sol"}
                }
            }
            self.assertEqual(self.route("gpt-6.2-sol")["model"], "")

    def test_speech_and_text_catalog_entries_cannot_be_cross_assigned(self):
        self.assertEqual(
            self.route("google/gemini-4-carbon", "transcription")["model"], ""
        )
        self.assertEqual(self.route("openai/gpt-transcribe")["model"], "")
        audio = self.route("openai/gpt-transcribe", "transcription")
        self.assertEqual(audio["api"], "audio-transcriptions")
        self.assertTrue(
            endpoint_and_headers(audio)[0].endswith("/openai/audio/transcriptions")
        )

    def test_installation_speech_defaults_and_choices_use_catalog_adapters(self):
        model = "@cf/deepgram/nova-3"
        legacy = "@cf/openai/whisper-large-v3-turbo"
        for default in (model, legacy):
            with (
                self.subTest(default=default),
                override_settings(
                    PANDECT_AI_GATEWAY=CONFIG
                    | {
                        "models": CONFIG["models"] | {"transcription": default},
                        "model_choices": {"transcription": (model, legacy)},
                    }
                ),
            ):
                operation = "transcription"
                self.case.external_analysis_models = {}
                default_route = gateway_execution(operation, operation, case=self.case)
                self.assertEqual(default_route["model"], default)
                if default == model:
                    self.assertEqual(default_route["api"], "nova-3")
                self.assertEqual(
                    self.route(pin_model_choice(operation, model), operation)["api"],
                    "nova-3",
                )
                self.assertEqual(
                    self.route(legacy, operation)["adapter_version"], "gateway-3"
                )
                choices = [
                    row
                    for row in external_model_options(operation)
                    if row["value"] == model
                ]
                self.assertEqual(len(choices), 1)
                self.assertTrue(choices[0]["available"])

    def test_binary_and_unified_speech_requests_use_the_reviewed_audio_route(self):
        wav = wav_bytes(seconds=0.25)
        payload = {"audio": b64encode(wav).decode(), "language": "en"}
        recording = AudioEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Synthetic audio",
            file=ContentFile(wav, name="sample.wav"),
        )
        for model, reply in [
            (
                "@cf/deepgram/nova-3",
                {
                    "success": True,
                    "result": {
                        "results": {
                            "channels": [
                                {
                                    "alternatives": [
                                        {
                                            "transcript": "Sample",
                                            "words": [
                                                {
                                                    "word": "Sample",
                                                    "start": 0,
                                                    "end": 0.25,
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ]
                        },
                    },
                },
            ),
            (
                "@cf/openai/whisper",
                {
                    "success": True,
                    "result": {
                        "text": "Sample",
                        "words": [{"word": "Sample", "start": 0, "end": 0.25}],
                    },
                },
            ),
            ("xai/grok-stt", {"state": "Completed", "result": {"text": "Sample"}}),
        ]:
            with self.subTest(model=model):
                self.route(model, "transcription")
                self.case.save(update_fields=["external_analysis_models"])
                run, _ = start_run(
                    self.users["owner"],
                    self.case.pk,
                    "transcription",
                    [{"kind": "audio", "id": str(recording.pk)}],
                    parameters={"backend": "gateway"},
                    **external_grant_kwargs(self.case, "transcription"),
                )
                client = GatewayClient(run, partial(authorize_run, run))
                with patch("core.analysis.gateway.build_opener") as build:
                    build.return_value.open.return_value = response(reply)
                    transcript = client.request("transcription", payload)
                    self.assertEqual(transcript["text"], "Sample")
                    request = build.return_value.open.call_args.args[0]
                    headers = {k.lower(): v for k, v in request.header_items()}
                    self.assertEqual(headers["cf-aig-gateway-id"], CONFIG["gateway"])
                    if model.startswith("@cf/"):
                        self.assertTrue(request.full_url.endswith("/ai/run/" + model))
                        self.assertEqual(request.data, wav)
                        self.assertEqual(transcript["segments"][0]["end"], 0.25)
                    else:
                        self.assertTrue(request.full_url.endswith("/ai/run"))
                        self.assertEqual(json.loads(request.data)["model"], model)
                        build.return_value.open.return_value = response(
                            {"state": "Processing", "result": {"text": "Partial"}}
                        )
                        with self.assertRaisesMessage(
                            AnalysisError, "gateway_incomplete_response"
                        ):
                            client.request("transcription", payload)

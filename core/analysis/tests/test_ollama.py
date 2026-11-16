"""Ollama identity, local-only dispatch, bounds and retained run behavior."""

import json
from copy import deepcopy
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, override_settings

from core.analysis.errors import AnalysisError
from core.analysis.execution import snapshot_execution
from core.analysis.local import LocalClient

EXECUTION = {
    "backend": "local",
    "purpose": "text",
    "driver": "ollama",
    "url": "http://127.0.0.1:11434",
    "model": "qwen3.5:9b",
    "sha256": "a" * 64,
    "runtime": "ollama@0.40.11",
    "context_tokens": 2048,
    "max_output_tokens": 64,
    "timeout": 12,
    "max_request_bytes": 65536,
    "max_response_bytes": 65536,
}
MODEL = f"sha256:{EXECUTION['sha256']}:local"
VERSION = {"version": "0.40.11"}
TAGS = {"models": [{"name": "qwen3.5:9b", "digest": "a" * 64}]}
INFO = {"details": {"family": "qwen3_5"}, "capabilities": ["completion"]}
RENDER = {"_debug_info": {"rendered_template": "Synthetic fully rendered prompt"}}
COMPLETION = {
    "model": MODEL,
    "message": {"role": "assistant", "content": '{"items":[]}'},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 20,
    "eval_count": 8,
}


class OllamaTests(SimpleTestCase):
    def setUp(self):
        self.authorize = Mock()
        self.client = LocalClient(self.authorize, dict(EXECUTION))
        self.replies = deepcopy(
            [VERSION, TAGS, INFO, RENDER, VERSION, COMPLETION, VERSION]
        )
        self.transport = self.enterContext(patch("core.analysis.local.build_opener"))
        self.transport.return_value.open.side_effect = self.respond
        self.cloud = self.enterContext(
            patch("core.analysis.gateway.GatewayClient.request")
        )
        self.requests = []

    def respond(self, request, **kwargs):
        self.requests.append(request)
        value = self.replies.pop(0)
        return BytesIO(
            value if isinstance(value, bytes) else json.dumps(value).encode()
        )

    def assert_failure(self, code):
        with self.assertRaisesMessage(AnalysisError, code):
            self.client.infer("Synthetic source", {"type": "object"})
        self.cloud.assert_not_called()

    def test_native_dispatch_pins_digest_and_validates_structured_output(self):
        schema = {"type": "object", "properties": {"items": {"type": "array"}}}
        self.assertEqual(self.client.infer("Synthetic source", schema), {"items": []})
        bodies = [json.loads(r.data) for r in self.requests if r.data]
        self.assertEqual(bodies[0], {"model": MODEL})
        rendered, generated = bodies[1:]
        self.assertTrue(rendered.pop("_debug_render_only"))
        self.assertEqual(rendered, generated)
        self.assertEqual(generated["model"], MODEL)
        self.assertEqual(generated["format"], schema)
        for name in ("think", "stream", "truncate", "shift"):
            self.assertIs(generated[name], False)
        self.assertEqual(
            generated["options"], {"temperature": 0, "num_ctx": 2048, "num_predict": 64}
        )
        self.assertEqual(generated["keep_alive"], 0)
        self.assertIn("untrusted data", generated["messages"][0]["content"])
        self.assertEqual(self.client.usage["input_tokens"], 20)
        self.assertEqual(self.client.usage["output_tokens"], 8)
        self.assertEqual(self.client.usage["request_count"], 7)
        self.assertEqual(self.authorize.call_count, 14)
        self.assertTrue(
            all(not r.has_header("X-pandect-identity") for r in self.requests)
        )
        self.cloud.assert_not_called()

    def test_changed_or_missing_model_is_refused_before_sending_evidence(self):
        for tags in (
            {"models": []},
            {"models": [{"name": "qwen3.5:9b", "digest": "b" * 64}]},
            {"models": [TAGS["models"][0] | {"remote_host": "https://cloud.test"}]},
        ):
            with self.subTest(tags=tags):
                self.replies = deepcopy([VERSION, tags])
                self.requests.clear()
                self.assert_failure("local_ai_model_changed")
                self.assertTrue(all(r.data is None for r in self.requests))

    def test_unsupported_family_or_remote_manifest_is_refused(self):
        for info in (
            INFO | {"details": {"family": "unknown"}},
            INFO | {"remote_model": "cloud-model"},
            INFO | {"capabilities": []},
        ):
            with self.subTest(info=info):
                self.replies = deepcopy([VERSION, TAGS, info])
                self.requests.clear()
                self.assert_failure("local_ai_not_configured")
                self.assertFalse(
                    any(r.full_url.endswith("/api/chat") for r in self.requests)
                )

    def test_runtime_drift_before_render_before_generation_or_after_output_fails(self):
        for index in (0, 4, 6):
            with self.subTest(index=index):
                self.replies = deepcopy(
                    [VERSION, TAGS, INFO, RENDER, VERSION, COMPLETION, VERSION]
                )
                self.replies[index] = {"version": "0.40.12"}
                self.assert_failure("local_ai_model_changed")

    def test_full_rendered_utf8_prompt_and_output_must_fit_without_truncation(self):
        self.replies[3] = {"_debug_info": {"rendered_template": "é" * 989}}
        self.assert_failure("local_ai_context_limit")
        self.assertEqual(len(self.requests), 4)
        self.assertTrue(json.loads(self.requests[-1].data)["_debug_render_only"])

    def test_missing_render_contract_fails_closed(self):
        self.replies[3] = deepcopy(COMPLETION)
        self.assert_failure("local_ai_invalid_response")
        self.assertEqual(len(self.requests), 4)

    def test_exact_rendered_context_boundary_can_generate(self):
        limit = EXECUTION["context_tokens"] - EXECUTION["max_output_tokens"] - 8
        self.replies[3] = {"_debug_info": {"rendered_template": "x" * limit}}
        self.assertEqual(self.client.infer("Synthetic source", {}), {"items": []})

    def test_missing_usage_is_rejected_without_fabricating_token_counts(self):
        for field in ("prompt_eval_count", "eval_count"):
            with self.subTest(field=field):
                self.replies = deepcopy(
                    [VERSION, TAGS, INFO, RENDER, VERSION, COMPLETION, VERSION]
                )
                self.replies[5].pop(field)
                self.assert_failure("local_ai_invalid_response")

    def test_malformed_partial_reasoning_and_remote_outputs_are_rejected(self):
        for changes in (
            {"done": False},
            {"done_reason": "length"},
            {"model": "qwen3.5:9b"},
            {"remote_host": "https://cloud.test"},
            {"message": None},
            {
                "message": {
                    "role": "assistant",
                    "content": "{}",
                    "thinking": "reasoning",
                }
            },
            {"message": {"role": "assistant", "content": "{}", "tool_calls": [{}]}},
            {"message": {"role": "assistant", "content": '{"x":NaN}'}},
            {"message": {"role": "assistant", "content": '{"x":1e999}'}},
            {"message": {"role": "assistant", "content": "invalid JSON"}},
            {"message": {"role": "assistant", "content": '"plain string"'}},
            {"prompt_eval_count": True},
            {"eval_count": 65},
            {"prompt_eval_count": 2048},
        ):
            with self.subTest(changes=changes):
                self.replies = deepcopy(
                    [
                        VERSION,
                        TAGS,
                        INFO,
                        RENDER,
                        VERSION,
                        COMPLETION | changes,
                        VERSION,
                    ]
                )
                self.assert_failure("local_ai_invalid_response")

    def test_bounded_http_errors_have_no_retry_or_cloud_fallback(self):
        for error, code in (
            (
                HTTPError(EXECUTION["url"], 404, "private detail", {}, None),
                "local_ai_model_changed",
            ),
            (
                HTTPError(EXECUTION["url"], 429, "private detail", {}, None),
                "local_ai_busy",
            ),
            (URLError("private detail"), "local_ai_unavailable"),
        ):
            with self.subTest(code=code):
                self.transport.return_value.open.reset_mock()
                self.transport.return_value.open.side_effect = error
                self.assert_failure(code)
                self.assertEqual(self.transport.return_value.open.call_count, 1)

    def test_response_and_request_limits_remain_enforced(self):
        self.replies[0] = b"x" * 65537
        self.assert_failure("local_ai_output_limit")
        self.replies = deepcopy([VERSION, TAGS, INFO])
        with self.assertRaisesMessage(AnalysisError, "local_ai_input_limit"):
            self.client.infer("x" * 65536, {})

    def test_revoked_authorization_stops_the_next_request(self):
        self.authorize.side_effect = PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.client.infer("Synthetic source", {})
        self.transport.return_value.open.assert_not_called()
        self.cloud.assert_not_called()

    def test_unsupported_driver_model_or_unpinned_runtime_is_unavailable(self):
        for change in (
            {"driver": "other"},
            {"driver": "llama.cpp"},
            {"driver": None},
            {"runtime": "ollama"},
            {"runtime": "ollama@0.17.7"},
            {"model": "qwen3.5:9b:cloud"},
        ):
            with self.subTest(change=change):
                with self.assertRaisesMessage(AnalysisError, "local_ai_not_configured"):
                    LocalClient(self.authorize, EXECUTION | change)

    def test_snapshot_records_driver_identity_and_decoding_without_credentials(self):
        text = dict(EXECUTION)
        text.pop("driver")
        config = {
            "text": text,
            "token": "synthetic-secret",
            **{
                key: EXECUTION[key]
                for key in (
                    "timeout",
                    "max_request_bytes",
                    "max_response_bytes",
                    "max_output_tokens",
                )
            },
        }
        with override_settings(PANDECT_LOCAL_AI=config):
            execution = snapshot_execution("content_events", {})
        self.assertEqual(execution["driver"], "ollama")
        self.assertEqual(execution["adapter_version"], "ollama-1")
        self.assertEqual(execution["sha256"], "a" * 64)
        self.assertFalse(execution["inference_settings"]["think"])
        self.assertEqual(execution["location"], "local")
        self.assertNotIn("synthetic-secret", str(execution))

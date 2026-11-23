"""Synthetic Cloudflare/OpenAI protocol, usage and authorization boundaries."""

import json
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from core.analysis.errors import AnalysisError
from core.analysis.http import NoRedirect
from core.analysis.tests.test_gateway import CONFIG, error, response
from core.case_search.semantic_model import embed, execution, identity, profile
from core.models import GatewayRequest
from core.testing import EvidenceViewTestMixin

GATEWAY = CONFIG | {"openai_key_alias": "search-test"}
MODEL = "text-embedding-3-large"
UNIT = [1] + [0] * 1535
UNIT2 = [0, 1] + [0] * 1534
OPENER = "core.case_search.hosted_embeddings.build_opener"


def reply(*vectors):
    return {
        "model": MODEL,
        "data": [
            {"index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
    }


@override_settings(
    PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway", PANDECT_AI_GATEWAY=GATEWAY
)
class HostedEmbeddingTests(EvidenceViewTestMixin, TestCase):
    def request(self, *texts, purpose="index", authorize=None):
        return embed(
            list(texts),
            authorize or (lambda: self.case),
            case=self.case,
            purpose=purpose,
        )

    def test_reviewed_identity_binds_destination_and_excludes_credentials(self):
        original = profile()
        self.assertEqual(len(original), 64)
        self.assertEqual(
            identity(execution()),
            {
                "provider": "openai-gateway",
                "model": MODEL,
                "dimensions": 1536,
                "account": "a" * 32,
                "gateway": GATEWAY["gateway"],
                "key_alias": "search-test",
                "chunker": "utf8-word-boundary-1",
                "chunk_bytes": 246,
            },
        )
        for config in (
            {"account": "b" * 32},
            {"gateway": "other"},
            {"openai_key_alias": "new"},
        ):
            with (
                self.subTest(config=config),
                override_settings(PANDECT_AI_GATEWAY=GATEWAY | config),
            ):
                self.assertNotEqual(profile(), original)
        with override_settings(
            PANDECT_AI_GATEWAY=GATEWAY | {"gateway_token": "rotated"}
        ):
            self.assertEqual(profile(), original)
        with (
            override_settings(PANDECT_AI_GATEWAY=GATEWAY | {"gateway_token": ""}),
            patch(OPENER) as build,
        ):
            self.assertEqual(profile(), "")
            with self.assertRaisesMessage(AnalysisError, "gateway_not_configured"):
                self.request("Synthetic")
            build.assert_not_called()
        with override_settings(PANDECT_SEARCH_EMBEDDING_PROVIDER="disabled"):
            self.assertEqual(profile(), "")

    def test_bounded_gateway_post_orders_by_provider_index_and_accounts_separately(
        self,
    ):
        value = reply(UNIT, UNIT2)
        value["data"].reverse()
        with patch(OPENER) as build:
            build.return_value.open.return_value = response(value)
            self.assertEqual(self.request("First", "Second"), [UNIT, UNIT2])
            request = build.return_value.open.call_args.args[0]
            self.assertIsInstance(build.call_args.args[0], NoRedirect)
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 15)
            self.assertEqual(
                request.full_url,
                f"https://gateway.ai.cloudflare.com/v1/{GATEWAY['account']}/{GATEWAY['gateway']}/openai/embeddings",
            )
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(
                headers["cf-aig-authorization"], "Bearer synthetic-gateway-secret"
            )
            self.assertNotIn("authorization", headers)
            self.assertEqual(headers["cf-aig-byok-alias"], "search-test")
            self.assertEqual(headers["cf-aig-collect-log"], "false")
            self.assertEqual(headers["cf-aig-skip-cache"], "true")
            self.assertEqual(
                json.loads(request.data),
                {
                    "model": MODEL,
                    "dimensions": 1536,
                    "encoding_format": "float",
                    "input": ["First", "Second"],
                },
            )
            usage = GatewayRequest.objects.get(case=self.case, operation="search_index")
            self.assertIsNone(usage.run_id)
            self.assertEqual(
                (usage.status, usage.input_tokens, usage.output_tokens),
                ("complete", 7, 0),
            )
            self.assertEqual(usage.request_bytes, len(request.data))
            self.assertGreater(usage.response_bytes, 0)
            build.return_value.open.return_value = response(reply(UNIT))
            self.assertEqual(self.request("A query", purpose="query"), [UNIT])
            self.assertTrue(
                GatewayRequest.objects.filter(
                    case=self.case, operation="search_query"
                ).exists()
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GatewayRequest.objects.create(case=self.case, request_bytes=5, model=MODEL)

    def test_schema_indices_size_and_unit_vectors_are_checked_before_use(self):
        for value in (
            reply(UNIT) | {"model": "different"},
            reply(),
            reply([1, 0]),
            reply([0] * 1536),
            reply([True] + UNIT[1:]),
            reply([float("nan")] + UNIT[1:]),
            reply(UNIT) | {"data": [{"index": False, "embedding": UNIT}]},
            reply(UNIT) | {"data": [{"index": -1, "embedding": UNIT}]},
            b"malformed",
        ):
            with patch(OPENER) as build:
                build.return_value.open.return_value = response(value)
                with self.assertRaisesMessage(
                    AnalysisError, "gateway_invalid_response"
                ):
                    self.request("Synthetic")
        with patch(OPENER) as build:
            duplicate = reply(UNIT, UNIT2)
            duplicate["data"][1]["index"] = 0
            build.return_value.open.return_value = response(duplicate)
            with self.assertRaisesMessage(AnalysisError, "gateway_invalid_response"):
                self.request("First", "Second")
            build.return_value.open.return_value = response(
                b"x" * (2 * 1024 * 1024 + 1)
            )
            with self.assertRaisesMessage(AnalysisError, "gateway_output_limit"):
                self.request("Synthetic")
            with self.assertRaisesMessage(AnalysisError, "gateway_input_limit"):
                self.request("ç" * 124)
            self.assertEqual(build.return_value.open.call_count, 2)

    def test_service_errors_are_sanitized_and_http_429_quota_is_distinct(self):
        quota = HTTPError(
            "https://example.test",
            429,
            "private detail",
            {},
            BytesIO(b'{"error":{"code":"insufficient_quota"}}'),
        )
        for failure, code in (
            (quota, "quota"),
            (error(429), "rate_limit"),
            (error(502), "unavailable"),
            (error(401), "authentication"),
            (error(400), "provider_failure"),
            (TimeoutError(), "unavailable"),
        ):
            with self.subTest(code=code), patch(OPENER) as build:
                build.return_value.open.side_effect = failure
                with self.assertRaisesMessage(AnalysisError, "gateway_" + code):
                    self.request("Synthetic")
                self.assertEqual(
                    GatewayRequest.objects.latest("created_at").status, code
                )
                self.assertEqual(build.return_value.open.call_count, 1)

    def test_case_and_destination_are_checked_at_transfer_and_after_receipt(self):
        with patch(OPENER) as build:
            with self.assertRaisesMessage(AnalysisError, "access_removed"):
                self.request("Synthetic", authorize=lambda: self.other_case)
            self.assertFalse(GatewayRequest.objects.exists())
            with self.assertRaises(PermissionDenied):
                self.request(
                    "Synthetic",
                    authorize=Mock(side_effect=[self.case, PermissionDenied]),
                )
            build.assert_not_called()
            build.return_value.open.return_value = response(reply(UNIT))
            with self.assertRaises(PermissionDenied):
                self.request(
                    "Synthetic",
                    authorize=Mock(
                        side_effect=[self.case, self.case, PermissionDenied]
                    ),
                )
            usage = GatewayRequest.objects.latest("created_at")
            self.assertEqual((usage.status, usage.input_tokens), ("access_removed", 7))
            with patch(
                "core.case_search.semantic_model.execution",
                side_effect=[execution(), {}],
            ):
                with self.assertRaisesMessage(AnalysisError, "execution_changed"):
                    self.request("Synthetic")
            self.assertEqual(build.return_value.open.call_count, 1)

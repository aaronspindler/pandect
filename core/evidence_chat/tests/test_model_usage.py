import json
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings

from core.analysis import chat_usage
from core.analysis.errors import AnalysisError
from core.analysis.execution import snapshot_execution
from core.analysis.gateway import GatewayClient
from core.analysis.local import LocalClient
from core.analysis.tests.test_gateway import CONFIG, error, response
from core.analysis.tests.test_ollama import (
    COMPLETION,
    EXECUTION,
    INFO,
    RENDER,
    TAGS,
    VERSION,
)
from core.case_search.rerank import HOSTED_MODEL, rerank_order
from core.case_search.semantic_model import embed
from core.case_search.tests.test_hosted_embeddings import GATEWAY, UNIT, reply
from core.evidence_chat.retrieval_route import related_for_turn
from core.models import ChatTurn, EvidenceChat
from core.testing import EvidenceViewTestMixin


@override_settings(PANDECT_AI_GATEWAY=CONFIG)
class ChatUsageTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        owner = self.users["owner"]
        chat = EvidenceChat.objects.create(case=self.case, created_by=owner)
        self.turn = ChatTurn.objects.create(
            case=self.case,
            chat=chat,
            asked_by=owner,
            ordinal=1,
            submission_id=uuid4(),
            status="running",
            execution=snapshot_execution(
                "evidence_chat", {"backend": "gateway"}, case=self.case
            ),
        )
        self.gateway = GatewayClient(
            None, lambda **kwargs: self.case, chat_turn=self.turn
        )

    def test_gateway_reconciles_totals_and_clamps_each_attempt(self):
        def result(request, *, timeout):
            limits.append(json.loads(request.data)["max_tokens"])
            return response(
                {
                    "response": "{}",
                    "usage": {"prompt_tokens": 9, "completion_tokens": 5},
                }
            )

        limits = []
        with (
            patch.object(chat_usage, "MAX_OUTPUT", 9),
            patch("core.analysis.gateway.build_opener") as transport,
        ):
            transport.return_value.open.side_effect = result
            self.gateway.infer("Find an event.", {})
            with self.assertRaisesMessage(AnalysisError, "gateway_output_limit"):
                self.gateway.infer("Follow the source.", {})
            with self.assertRaisesMessage(AnalysisError, "chat_budget_exhausted"):
                self.gateway.infer("Anything else?", {})
        self.turn.refresh_from_db()
        self.assertEqual(limits, [9, 4])
        self.assertEqual([row["input_tokens"] for row in self.turn.model_usage], [9, 9])
        self.assertNotIn("Find an event", str(self.turn.model_usage))

    def test_provider_retry_and_redelivery_preserve_unknown_usage(self):
        with (
            patch.object(chat_usage, "MAX_ATTEMPTS", 1),
            patch("core.analysis.gateway.build_opener") as transport,
            patch("core.analysis.gateway.time.sleep"),
        ):
            transport.return_value.open.side_effect = error(503)
            with self.assertRaisesMessage(AnalysisError, "chat_budget_exhausted"):
                self.gateway.infer("Find an event.", {})
            self.turn.refresh_from_db()
            self.turn.delivery_token = uuid4()
            self.turn.save(update_fields=["delivery_token"])
            with self.assertRaisesMessage(AnalysisError, "chat_budget_exhausted"):
                self.gateway.infer("Find an event.", {})
        self.assertEqual(transport.return_value.open.call_count, 1)
        self.assertEqual(len(self.turn.model_usage), 1)
        self.assertNotIn("input_tokens", self.turn.model_usage[0])
        self.assertGreater(self.turn.model_usage[0]["output_limit"], 0)

    def test_local_preflight_and_actual_generation_and_auxiliary_share_allowance(self):
        requests = []
        replies = iter([VERSION, TAGS, INFO, RENDER, VERSION, COMPLETION, VERSION])

        def send(request, **kwargs):
            requests.append(request)
            return BytesIO(json.dumps(next(replies)).encode())

        client = LocalClient(lambda: self.case, EXECUTION, chat_turn=self.turn)
        with (
            patch.object(chat_usage, "MAX_OUTPUT", 12),
            patch("core.analysis.local.build_opener") as transport,
        ):
            transport.return_value.open.side_effect = send
            self.assertEqual(client.infer("Synthetic question.", {}), {"items": []})
        self.assertEqual(json.loads(requests[-2].data)["options"]["num_predict"], 12)
        self.turn.refresh_from_db()
        render, answer = self.turn.model_usage
        self.assertEqual((render["purpose"], render["input_bound"]), ("render", 0))
        self.assertEqual((answer["input_tokens"], answer["output_tokens"]), (20, 8))
        with (
            patch.object(chat_usage, "MAX_ATTEMPTS", 2),
            self.assertRaisesMessage(AnalysisError, "chat_budget_exhausted"),
        ):
            chat_usage.reserve(self.turn, "rerank", 23)

    def test_reservation_reconciles_once_and_verifies_delivery(self):
        reservation = chat_usage.reserve(self.turn, "embedding", 15)
        reservation.settle(9, 0)
        reservation.settle(2, 0)
        self.turn.refresh_from_db()
        self.assertEqual(self.turn.model_usage[0]["input_tokens"], 9)
        self.turn.delivery_token = uuid4()
        with self.assertRaisesMessage(AnalysisError, "processing_cancelled"):
            chat_usage.reserve(self.turn, "answer", 1, output_limit=3)

    @override_settings(
        PANDECT_SEARCH_EMBEDDING_PROVIDER="openai-gateway", PANDECT_AI_GATEWAY=GATEWAY
    )
    def test_embedding_uses_final_attempt_and_optional_rerank_keeps_existing_order(
        self,
    ):
        scope = {
            "provider": "workers-ai",
            "model": HOSTED_MODEL,
            "account": GATEWAY["account"],
            "gateway": GATEWAY["gateway"],
        }
        self.turn.plan = {"rerank_scope": scope}
        self.turn.save(update_fields=["plan"])
        with (
            patch.object(chat_usage, "MAX_ATTEMPTS", 1),
            patch("core.case_search.hosted_embeddings.build_opener") as embedding,
            patch("core.case_search.rerank.rerank_scope", return_value=scope),
            patch("core.case_search.rerank.build_opener") as rerank,
        ):
            embedding.return_value.open.return_value = response(reply(UNIT))
            embed(
                ["Delivery"],
                lambda: self.case,
                purpose="query",
                case=self.case,
                chat_turn=self.turn,
            )
            self.assertEqual(
                rerank_order(
                    self.case,
                    "Delivery",
                    ["Harbour dispatch register one", "Harbour dispatch register two"],
                    scope=scope,
                    authorize=lambda: self.case,
                    chat_turn=self.turn,
                ),
                [0, 1],
            )
            rerank.assert_not_called()
            with self.assertRaisesMessage(AnalysisError, "chat_budget_exhausted"):
                embed(
                    ["Follow-up"],
                    lambda: self.case,
                    purpose="query",
                    case=self.case,
                    chat_turn=self.turn,
                )
            self.assertEqual(embedding.return_value.open.call_count, 1)
        self.turn.refresh_from_db()
        (entry,) = self.turn.model_usage
        self.assertEqual((entry["purpose"], entry["input_tokens"]), ("embedding", 7))

    def test_unaffordable_embedding_falls_back_to_saved_results(self):
        with (
            patch("core.evidence_chat.retrieval_route.profile", return_value="local"),
            patch("core.evidence_chat.retrieval_route.is_external", return_value=False),
            patch("core.evidence_chat.retrieval_route.related_results") as related,
            patch("core.evidence_chat.retrieval_route.parent_results") as parents,
        ):
            related.side_effect = AnalysisError("chat_budget_exhausted")
            parents.return_value = [{"id": "saved"}]
            self.assertEqual(
                related_for_turn(self.turn, "Delivery", lambda: self.case),
                ([{"id": "saved"}], ""),
            )
            related.side_effect = AnalysisError("access_removed")
            with self.assertRaisesMessage(AnalysisError, "access_removed"):
                related_for_turn(self.turn, "Delivery", lambda: self.case)
            parents.assert_called_once()

    def test_separate_input_transfer_bounds_and_full_review(self):
        for setting, limit in (("MAX_INPUT", 10), ("MAX_TRANSFER_BYTES", 6)):
            with (
                self.subTest(setting=setting),
                patch.object(chat_usage, setting, limit),
            ):
                with self.assertRaisesMessage(AnalysisError, "chat_budget_exhausted"):
                    chat_usage.reserve(self.turn, "answer", 11, output_limit=3)
        self.turn.refresh_from_db()
        self.assertEqual(self.turn.model_usage, [])
        self.turn.mode = "full"
        self.turn.save(update_fields=["mode"])
        with patch.object(chat_usage, "MAX_ATTEMPTS", 0):
            self.assertIsNone(
                chat_usage.reserve(self.turn, "answer", 30, output_limit=3)
            )

"""Decisions dispatch, current consent, typed responses, and cumulative cost."""

import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import TestCase, override_settings

from core.analysis import chat_usage
from core.analysis.errors import AnalysisError
from core.analysis.http import NoRedirect
from core.analysis.tests.test_gateway import error, response
from core.evidence_chat import decisions, decisions_approval
from core.models import ChatRoutingApproval, ChatTurn, EvidenceChat
from core.testing import EvidenceViewTestMixin


@override_settings(PANDECT_DIRECT_AI={"openai_api_key": "test-server-key"})
class DecisionsTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        actor = self.users["owner"]
        self.grant = ChatRoutingApproval.objects.create(
            case=self.case, destination=decisions.destination(), changed_by=actor
        )
        self.chat = EvidenceChat.objects.create(case=self.case, created_by=actor)
        self.rows = [
            {"type": "original/text", "label": "Gate receipt", "preview": "09:00"},
            {"type": "finding", "label": "Clock discrepancy", "preview": "10:00"},
        ]
        self.authorize = Mock(return_value=self.case)
        self.opener = self.enterContext(patch.object(decisions, "build_opener"))
        self.http = self.opener.return_value.open

    def turn(self):
        return ChatTurn.objects.create(
            case=self.case,
            chat=self.chat,
            asked_by=self.users["editor"],
            ordinal=self.chat.turns.count() + 1,
            question="Compare the two gate times",
            submission_id=uuid4(),
            status="running",
            plan={"decisions_scope": decisions_approval.scope(self.case)},
        )

    def run_route(self, turn):
        return decisions.choose_source(turn, self.rows, self.authorize)

    def reply(self, **answer):
        return {
            "model": decisions.MODEL,
            "answers": [
                {
                    "type": "choice",
                    "name": "next_source",
                    "choice": "source_1",
                    "confidence": 0.83,
                    **answer,
                }
            ],
            "usage": {
                "input_tokens": 38,
                "output_tokens": 0,
                "input_tokens_details": {"cached_tokens": 6},
            },
        }

    def test_only_bounded_descriptions_leave_case_and_usage_is_settled(self):
        turn = self.turn()
        self.rows[0].update(label="A" * 140, preview="P" * 300, locator="private-id")
        self.http.return_value = response(self.reply())
        self.assertEqual(self.run_route(turn), 1)
        request = self.http.call_args.args[0]
        self.assertIsInstance(self.opener.call_args.args[0], NoRedirect)
        self.assertEqual(request.full_url, decisions.URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-server-key")
        payload = json.loads(request.data)
        data = payload["input"][0]["content"][0]["data"]
        self.assertEqual(data["question"], turn.question)
        self.assertEqual(
            data["candidates"][0],
            {"type": "original/text", "label": "A" * 120, "preview": "P" * 200},
        )
        self.assertEqual(payload["prompt_cache_options"], {"mode": "explicit"})
        self.assertNotIn(b"private-id", request.data)
        self.assertNotIn(str(turn.pk).encode(), request.data)
        usage = turn.gateway_requests.get()
        self.assertEqual(usage.case_id, self.case.pk)
        self.assertEqual((usage.input_tokens, usage.output_tokens), (38, 0))
        self.assertEqual((usage.status, usage.model), ("complete", decisions.MODEL))
        turn.refresh_from_db()
        (entry,) = turn.model_usage
        self.assertEqual(entry["purpose"], "decisions")
        self.assertEqual(entry["bytes"], len(request.data))
        self.assertEqual(entry["input_tokens"], 38)
        self.assertNotIn("Gate receipt", str(entry))
        # Ambiguous redelivery uses the original ranking, never another request.
        self.assertIsNone(self.run_route(turn))
        self.http.assert_called_once()

    def test_unavailable_and_oversized_responses_keep_the_reservation(self):
        for failure, status in [
            (TimeoutError(), "unavailable"),
            (error(429), "rate_limit"),
            (b"x" * (decisions.MAX_RESPONSE_BYTES + 1), "output_limit"),
            (b"not JSON", "invalid_response"),
        ]:
            with self.subTest(status=status):
                turn = self.turn()
                self.http.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                if isinstance(failure, bytes):
                    self.http.return_value = response(failure)
                self.assertIsNone(self.run_route(turn))
                self.assertEqual(turn.gateway_requests.get().status, status)
                turn.refresh_from_db()
                self.assertNotIn("input_tokens", turn.model_usage[0])
                turn.delivery_token = uuid4()
                turn.save(update_fields=["delivery_token"])
                self.assertIsNone(self.run_route(turn))
                self.assertEqual(turn.gateway_requests.count(), 1)
                turn.status = "complete"
                turn.save(update_fields=["status"])

    def test_refusal_unlisted_choice_wrong_model_and_uncertainty(self):
        for result, status in [
            (self.reply(choice="keep"), "abstained"),
            (self.reply(confidence=0.1), "abstained"),
            (self.reply(choice="source_2"), "invalid_response"),
            (self.reply(choice=True), "invalid_response"),
            (self.reply(name="run_analysis"), "invalid_response"),
            (self.reply() | {"model": "other"}, "invalid_response"),
            (
                self.reply()
                | {"answers": [{"type": "refusal", "name": "next_source"}]},
                "refused",
            ),
        ]:
            with self.subTest(result=result):
                turn = self.turn()
                self.http.return_value = response(result)
                self.assertIsNone(self.run_route(turn))
                receipt = turn.gateway_requests.get()
                self.assertEqual(receipt.status, status)
                if status == "invalid_response":
                    self.assertIsNone(receipt.input_tokens)
                turn.status = "complete"
                turn.save(update_fields=["status"])

    def test_source_access_and_grant_rechecked_on_error_and_success(self):
        turn = self.turn()
        self.http.side_effect = TimeoutError()
        self.authorize.side_effect = [
            self.case,
            self.case,
            AnalysisError("source_changed"),
        ]
        with self.assertRaisesMessage(AnalysisError, "source_changed"):
            self.run_route(turn)
        self.assertEqual(turn.gateway_requests.get().status, "cancelled")
        turn.status = "complete"
        turn.save(update_fields=["status"])
        turn = self.turn()
        self.authorize.side_effect = None

        def revoke(request, **kwargs):
            self.grant.delete()
            return response(self.reply())

        self.http.side_effect = revoke
        self.assertIsNone(self.run_route(turn))
        usage = turn.gateway_requests.get()
        self.assertEqual((usage.status, usage.input_tokens), ("cancelled", 38))

    def test_disabled_changed_oversized_and_unaffordable_skip_dispatch(self):
        turn = self.turn()
        with self.settings(PANDECT_DIRECT_AI={}):
            self.assertFalse(decisions.destination())
            self.assertIsNone(self.run_route(turn))
        with patch.object(chat_usage, "MAX_ATTEMPTS", 1):
            self.assertIsNone(self.run_route(turn))
        with patch.object(chat_usage, "MAX_INPUT", 1):
            self.assertIsNone(self.run_route(turn))
        self.assertIsNone(decisions.choose_source(turn, self.rows * 5, self.authorize))
        self.grant.revision = uuid4()
        self.grant.save(update_fields=["revision"])
        self.assertIsNone(self.run_route(turn))
        self.assertFalse(turn.gateway_requests.exists())
        self.http.assert_not_called()
        self.authorize.return_value = self.other_case
        with self.assertRaisesMessage(AnalysisError, "access_removed"):
            self.run_route(turn)

    def test_stale_delivery_propagates_before_http(self):
        turn = self.turn()
        turn.delivery_token = uuid4()
        with self.assertRaisesMessage(AnalysisError, "processing_cancelled"):
            self.run_route(turn)
        self.http.assert_not_called()

    def test_success_without_usage_keeps_estimates_and_no_token_receipt(self):
        turn = self.turn()
        result = self.reply()
        del result["usage"]
        self.http.return_value = response(result)
        self.assertEqual(self.run_route(turn), 1)
        turn.refresh_from_db()
        (entry,) = turn.model_usage
        self.assertGreater(entry["input_bound"], entry["bytes"])
        self.assertGreater(entry["output_limit"], 0)
        self.assertNotIn("input_tokens", entry)
        self.assertNotIn("output_tokens", entry)
        receipt = turn.gateway_requests.get()
        self.assertIsNone(receipt.input_tokens)
        self.assertIsNone(receipt.output_tokens)

    def test_routing_preserves_input_and_transfer_headroom_for_an_answer(self):
        turn = self.turn()
        # Both limits can cover the routing request by itself. Reserving it
        # would leave less than an ordinary answer can use.
        with patch.object(chat_usage, "MAX_INPUT", chat_usage.ANSWER_HEADROOM_BYTES):
            self.assertIsNone(self.run_route(turn))
        with patch.object(
            chat_usage, "MAX_TRANSFER_BYTES", chat_usage.ANSWER_HEADROOM_BYTES
        ):
            self.assertIsNone(self.run_route(turn))
        turn.execution = {"backend": "local"}
        turn.save(update_fields=["execution"])
        with patch.object(
            chat_usage, "MAX_TRANSFER_BYTES", 2 * chat_usage.ANSWER_HEADROOM_BYTES
        ):
            self.assertIsNone(self.run_route(turn))
        self.http.assert_not_called()
        turn.refresh_from_db()
        self.assertEqual(turn.model_usage, [])

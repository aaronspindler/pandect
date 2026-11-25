import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from cases.search_settings import rerank_approval_context
from core.analysis.errors import AnalysisError
from core.analysis.tests.test_gateway import response
from core.case_search.rerank import (
    HOSTED_MODEL,
    LOCAL_MODEL,
    rerank_order,
    rerank_scope,
)
from core.models import ChatTurn, EvidenceChat, GatewayRequest
from core.testing import EvidenceViewTestMixin

OPENER = "core.case_search.rerank.build_opener"
LOCAL = {
    "url": "http://tei:80",
    "revision": "d" * 40,
    "version": "1.9.3",
    "token": "",
}


@override_settings(
    PANDECT_SEARCH_RERANK_PROVIDER="workers-ai",
    PANDECT_AI_GATEWAY={
        "account": "a" * 32,
        "gateway": "synthetic",
        "gateway_token": "fixture-gw",
        "workers_token": "fixture-workers",
    },
)
class RerankTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("cases:case_passage_ranking", args=(self.case.pk,))

    def change(self, action, *, token=None):
        review = rerank_approval_context(self.case, self.users["owner"])
        self.login("owner")
        result = self.client.post(
            self.url, {"action": action, "confirmation": token or review[action]}
        )
        self.assertEqual(result.status_code, 302)
        self.case.refresh_from_db()
        return rerank_scope(self.case)

    def authorize(self):
        self.case.refresh_from_db()
        return self.case

    def rank(
        self,
        scope,
        texts=("Harbour dispatch register delayed", "Harbour dispatch register sent"),
        *,
        authorize=None,
        turn=None,
    ):
        return rerank_order(
            self.case,
            "Q" * 300,
            texts,
            scope=scope,
            authorize=authorize or self.authorize,
            chat_turn=turn,
        )

    def test_only_owner_can_review_and_change_destination_and_grants_are_separate(self):
        for role, expected in (("editor", 403), ("viewer", 403), ("outsider", 404)):
            self.login(role)
            self.assertEqual(
                self.client.post(self.url, {"action": "approve"}).status_code, expected
            )
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.users["owner"])
        self.assertEqual(csrf.post(self.url, {"action": "approve"}).status_code, 403)
        self.login("owner")
        page = self.client.get(reverse("cases:case_settings", args=(self.case.pk,)))
        self.assertContains(page, "Approve hosted passage ranking")
        self.assertContains(page, HOSTED_MODEL)
        self.assertFalse(rerank_scope(self.case))
        stale = rerank_approval_context(self.case, self.users["owner"])["approve"]
        first = self.change("approve")
        self.assertFalse(self.case.search_embedding_approval)
        self.change("revoke")
        self.assertFalse(self.change("approve", token=stale))
        self.assertNotEqual(first, self.change("approve"))
        with self.settings(PANDECT_AI_GATEWAY={}):
            self.assertFalse(rerank_scope(self.case))
            self.assertContains(
                self.client.get(reverse("cases:case_settings", args=(self.case.pk,))),
                "inactive",
            )

    def test_hosted_payload_bounds_scores_and_usage(self):
        scope = self.change("approve")
        rows = [{"id": i, "score": i / 24} for i in range(24)]
        with patch(OPENER) as http:
            http.return_value.open.return_value = response(
                {"result": {"response": rows}, "success": True}
            )
            actual = self.rank(scope, ["harbour dispatch register " * 50] * 25)
        self.assertEqual(actual, [*reversed(range(24)), 24])
        request = http.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(
            request.full_url.endswith("/workers-ai/@cf/baai/bge-reranker-base")
        )
        self.assertEqual(request.get_header("Cf-aig-collect-log-payload"), "false")
        self.assertEqual(request.get_header("Cf-aig-skip-cache"), "true")
        payload = json.loads(request.data)
        self.assertEqual((len(payload["query"]), len(payload["contexts"])), (200, 24))
        self.assertEqual({len(c["text"]) for c in payload["contexts"]}, {900})
        usage = GatewayRequest.objects.get(case=self.case)
        self.assertEqual(
            (usage.status, usage.model, usage.operation),
            ("complete", HOSTED_MODEL, "search_query"),
        )

    def test_recheck_source_revocation_and_malformed_response(self):
        scope = self.change("approve")
        with patch(OPENER) as http:
            http.return_value.open.return_value = response(
                {"response": [{"id": 1, "score": 0.9}] * 2}
            )
            self.assertEqual(self.rank(scope), [0, 1])
            self.assertEqual(GatewayRequest.objects.get().status, "invalid_response")

            def revoke(*args, **kwargs):
                self.change("revoke")
                return response(
                    {"response": [{"id": 1, "score": 0.9}, {"id": 0, "score": 0.2}]}
                )

            http.return_value.open.side_effect = revoke
            self.assertEqual(self.rank(scope), [0, 1])
            self.assertEqual(
                GatewayRequest.objects.latest("created_at").status, "approval_changed"
            )
            self.change("approve")
            self.assertEqual(self.rank(scope), [0, 1])
            self.assertEqual(http.return_value.open.call_count, 2)
        scope = rerank_scope(self.case)
        with patch(OPENER) as http:
            validator = Mock(
                side_effect=[self.case, self.case, AnalysisError("source_changed")]
            )
            with self.assertRaisesMessage(AnalysisError, "source_changed"):
                self.rank(scope, authorize=validator)
            http.return_value.open.assert_not_called()

    def test_independent_matches_keep_rank_and_are_never_sent(self):
        scope = self.change("approve")
        documents = [
            "North Hall delivery register",
            "Car parked overnight",
            "North Hall delivery cancelled",
        ]
        with patch(OPENER) as http:
            self.assertEqual(self.rank(scope, documents[:2]), [0, 1])
            http.return_value.open.assert_not_called()
            http.return_value.open.return_value = response(
                {"response": [{"id": 1, "score": 0.9}, {"id": 0, "score": 0.3}]}
            )
            self.assertEqual(self.rank(scope, documents), [2, 1, 0])
            payload = json.loads(http.return_value.open.call_args.args[0].data)
            self.assertEqual(
                [c["text"] for c in payload["contexts"]], [documents[0], documents[2]]
            )

    def test_turn_reservation_survives_retry_and_unavailable_model(self):
        scope = self.change("approve")
        actor = self.users["editor"]
        turn = ChatTurn.objects.create(
            case=self.case,
            chat=EvidenceChat.objects.create(case=self.case, created_by=actor),
            asked_by=actor,
            status="running",
            ordinal=1,
            submission_id=uuid4(),
            question="Synthetic comparison",
            plan={"rerank_scope": scope},
        )
        with patch(OPENER) as http:
            http.return_value.open.side_effect = TimeoutError()
            self.assertEqual(self.rank(scope, turn=turn), [0, 1])
            turn.refresh_from_db()
            self.assertIsNotNone(turn.rerank_started_at)
            turn.delivery_token = uuid4()
            turn.save(update_fields=["delivery_token"])
            self.assertEqual(self.rank(scope, turn=turn), [0, 1])
            self.assertEqual(http.return_value.open.call_count, 1)
            self.assertEqual(turn.gateway_requests.get().status, "unavailable")

    @override_settings(
        PANDECT_SEARCH_RERANK_PROVIDER="local", PANDECT_LOCAL_RERANK=LOCAL
    )
    def test_pinned_local_identity_and_model_drift(self):
        scope = rerank_scope(self.case)
        info = {
            "model_id": LOCAL_MODEL,
            "version": LOCAL["version"],
            "model_sha": LOCAL["revision"],
            "model_type": {"reranker": {}},
        }
        results = [{"index": 1, "score": 0.8}, {"index": 0, "score": 0.2}]
        with patch(OPENER) as http:
            http.return_value.open.side_effect = [
                response(info),
                response(results),
                response(info),
            ]
            self.assertEqual(self.rank(scope), [1, 0])
            urls = [
                call.args[0].full_url for call in http.return_value.open.call_args_list
            ]
            self.assertEqual(
                urls,
                ["http://tei:80/info", "http://tei:80/rerank", "http://tei:80/info"],
            )
            http.return_value.open.reset_mock()
            http.return_value.open.side_effect = [response(info | {"model_sha": "bad"})]
            self.assertEqual(self.rank(scope), [0, 1])
            self.assertEqual(http.return_value.open.call_count, 1)
        self.assertFalse(GatewayRequest.objects.exists())

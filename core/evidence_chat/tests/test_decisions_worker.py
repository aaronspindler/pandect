"""Exercise optional source routing through the worker and both HTTP adapters."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings

from core.analysis.catalogue import project_result
from core.analysis.execution import configured_external_executions
from core.analysis.services import resolve_inputs, snapshot_sources
from core.analysis.testing import approve_case_execution
from core.analysis.tests.test_gateway import response
from core.analysis.tests.test_openai_gateway import OPENAI, openai_reply
from core.case_search.semantic_model import profile
from core.case_search.tests.test_rerank import LOCAL
from core.case_search.tests.test_semantic import EMBEDDING, UNIT, vectors
from core.evidence_chat import corpus, decisions_approval, jobs, services
from core.evidence_chat.tests.test_engine import decision
from core.models import (
    AnalysisResult,
    AnalysisRun,
    CaseSearchIndex,
    SearchDocument,
    SearchPassage,
)
from core.testing import EvidenceViewTestMixin
from people.models import Person
from phone.analysis.deterministic.conversations import interaction_patterns
from phone.models import TextMessageEvidence
from text.models import TextEvidence


def answer_from_quotes(records, quotes):
    claims = []
    for quote in quotes:
        record = next(row for row in records if quote in row["text"])
        citation = {
            "id": record["id"],
            "start": record["text"].index(quote),
            "quote": quote,
        }
        claims.append({"text": quote, "citations": [citation]})
    return decision("answer", claims=claims)


@override_settings(
    PANDECT_AI_GATEWAY=OPENAI,
    PANDECT_DIRECT_AI={"openai_api_key": "synthetic-routing-key"},
)
class DecisionsWorkerTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.users["owner"]
        self.case.ai_backend = "gateway"
        self.case.save(update_fields=["ai_backend"])
        approve_case_execution(
            self.case, self.owner, configured_external_executions(case=self.case)
        )
        self.routing_http = self.enterContext(
            patch("core.evidence_chat.decisions.build_opener")
        ).return_value.open
        self.answer_http = self.enterContext(
            patch("core.analysis.gateway.build_opener")
        ).return_value.open

    def routing_approval(self, action="approve"):
        review = decisions_approval.approval_context(self.case, self.owner)
        result = decisions_approval.change_approval(
            self.owner, self.case.pk, action, review[action]
        )
        self.assertEqual(result, action)

    def submit(self, question):
        return services.submit_turn(
            self.users["editor"], self.case.pk, question, submission_id=uuid4()
        )

    def answer(self, turn, make_answer):
        def reply(request, **kwargs):
            prompt = json.loads(json.loads(request.data)["input"][1]["content"])
            records = json.loads(prompt["task"].split("\n", 1)[1])["records"]
            # The synthetic receipt settles the turn's reservation so another
            # answer call can run. Only serialized request bytes are measured.
            return response(openai_reply(make_answer(records)))

        self.answer_http.side_effect = reply
        for _ in range(3):
            jobs.process_turn(turn.pk, turn.delivery_token)
            turn.refresh_from_db()
            if turn.status not in ("running", "queued"):
                break
        self.assertEqual(turn.status, "complete", turn.error_code)
        return services.inspect_turn(turn)

    def select_candidate(self, match):
        def reply(request, **kwargs):
            self.assertEqual(request.full_url, "https://api.openai.com/v1/decisions")
            payload = json.loads(request.data)
            candidates = payload["input"][0]["content"][0]["data"]["candidates"]
            self.assertLessEqual(len(candidates), 8)
            index = next(
                i for i, candidate in enumerate(candidates) if match(candidate)
            )
            return response(
                {
                    "model": "gpt-6-luna",
                    "answers": [
                        {
                            "name": "next_source",
                            "type": "choice",
                            "choice": f"source_{index}",
                            "confidence": 0.95,
                        }
                    ],
                }
            )

        self.routing_http.side_effect = reply

    def text_source(self, title, body):
        return TextEvidence.objects.create(
            case=self.case, uploaded_by=self.owner, title=title, body=body
        )

    def test_lower_ranked_conflicting_original_stays_citable_as_the_case_grows(self):
        question = "Compare accounts of the Kestrel departure."
        logged = "Kestrel departure was logged at 08:00."
        for number in range(5):
            self.text_source(f"Departure account {number}", f"{logged} Log {number}.")
        contrary = "Kestrel was still at the dock at 09:10."
        witness = self.text_source("Wharf witness", contrary)

        def first_four(records):
            self.assertFalse(any(contrary in r["text"] for r in records))
            return answer_from_quotes(records, [logged])

        self.answer(self.submit(question), first_four)
        self.routing_http.assert_not_called()
        self.routing_approval()
        self.select_candidate(lambda row: "Wharf witness" in row["label"])
        request_pairs = []
        for lower, upper in ((0, 1_000), (1_000, 10_000)):
            with self.subTest(distractors=upper):
                rows = []
                for n in range(lower, upper):
                    body = f"Box {n} contains linen."
                    rows.append(
                        TextEvidence(
                            case=self.case,
                            uploaded_by=self.owner,
                            title=f"Inventory {n}",
                            body=body,
                            sha256=sha256(body.encode()).hexdigest(),
                        )
                    )
                TextEvidence.objects.bulk_create(rows, batch_size=500)
                self.routing_http.reset_mock()
                self.answer_http.reset_mock()
                turn = self.submit(question)
                with patch.object(
                    corpus,
                    "discover",
                    side_effect=AssertionError("Unexpected full review"),
                ):
                    result = self.answer(
                        turn,
                        lambda records: answer_from_quotes(records, [logged, contrary]),
                    )
                self.routing_http.assert_called_once()
                self.answer_http.assert_called_once()
                self.assertEqual(turn.mode, "ordinary")
                quotes = [c["citations"][0] for c in result["claims"]]
                self.assertEqual([c["quote"] for c in quotes], [logged, contrary])
                self.assertIn(str(witness.pk), quotes[1]["url"])
                self.assertNotEqual(quotes[0]["url"], quotes[1]["url"])
                self.assertFalse(AnalysisRun.objects.filter(case=self.case).exists())
                request_pairs.append(
                    (
                        self.routing_http.call_args.args[0].data,
                        self.answer_http.call_args.args[0].data,
                    )
                )
        self.assertEqual(request_pairs[0], request_pairs[1])

    def save_message_timing_report(
        self, *, partner_name="Mica", opening="Parcel collected."
    ):
        partner = Person.objects.create(case=self.case, first_name=partner_name)
        stamp = datetime(2026, 2, 17, 10, tzinfo=UTC)
        originals = [
            TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                sender=sender,
                recipient=recipient,
                occurred_at=stamp + timedelta(minutes=minute),
                title="Recorded exchange",
                body=body,
            )
            for sender, recipient, minute, body in (
                (self.person, partner, 0, opening),
                (partner, self.person, 2, "Acknowledged."),
            )
        ]
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.owner,
            operation="comm_patterns",
            inputs=snapshot_sources(
                self.case, [{"kind": "text-message", "id": m.pk} for m in originals]
            ),
            method_version="2",
            status="complete",
        )
        saved = AnalysisResult.objects.create(
            case=self.case,
            run=run,
            data=interaction_patterns(resolve_inputs(run, self.case), {}),
        )
        project_result(saved)
        self.assertFalse(saved.findings.exists())
        return run

    def test_router_selects_saved_measurement_before_first_answer(self):
        run = self.save_message_timing_report()
        for n in range(5):
            self.text_source(f"Exchange register {n}", f"Recorded exchange number {n}.")
        quote = "Median response interval (seconds): 120.0"
        question = (
            "Compare the recorded exchange with its saved median response interval."
        )

        def read_saved_value(records):
            if any(quote in row["text"] for row in records):
                return answer_from_quotes(records, [quote])
            return decision(
                "read_timing", "median response interval", missing="The saved interval."
            )

        baseline = self.answer(self.submit(question), read_saved_value)
        self.routing_http.assert_not_called()
        self.assertEqual(self.answer_http.call_count, 2)
        baseline_bytes = sum(
            len(call.args[0].data) for call in self.answer_http.call_args_list
        )
        self.answer_http.reset_mock()

        self.routing_approval()
        self.select_candidate(lambda row: "timing" in row["label"].lower())
        turn = self.submit(question)
        result = self.answer(turn, read_saved_value)
        self.routing_http.assert_called_once()
        self.answer_http.assert_called_once()
        self.assertEqual(baseline["claims"], result["claims"])
        routed_bytes = len(self.routing_http.call_args.args[0].data) + len(
            self.answer_http.call_args.args[0].data
        )
        self.assertLess(routed_bytes, baseline_bytes)
        self.assertSequenceEqual(AnalysisRun.objects.filter(case=self.case), [run])
        citation = result["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], quote)
        self.assertIn(str(run.pk), citation["url"])
        self.assertIn("#processing-summary", citation["url"])

    def test_message_report_can_match_the_second_shortlisted_original(self):
        run = self.save_message_timing_report()
        self.save_message_timing_report(partner_name="Sable", opening="Lunch ordered.")
        lead = "Parcel notification timing was recorded here."
        TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            sender=self.person,
            recipient=self.person,
            title="Parcel notifications",
            body=lead,
        )
        self.routing_approval()
        quote = "Median response interval (seconds): 120.0"
        captured = []

        def cite_report(records):
            captured.extend(records)
            available = quote if any(quote in row["text"] for row in records) else lead
            return answer_from_quotes(records, [available])

        result = self.answer(
            self.submit("Compare parcel notification timing in these messages."),
            cite_report,
        )
        self.routing_http.assert_not_called()
        self.assertEqual(len(captured), 3)
        self.assertIn(lead, captured[0]["text"])
        self.assertIn("Parcel collected.", captured[1]["text"])
        self.assertIn("Mica", captured[2]["text"])
        self.assertNotIn("Sable", json.dumps(captured))
        citation = result["claims"][0]["citations"][0]
        self.assertIn(str(run.pk), citation["url"])
        self.assertEqual(citation["quote"], quote)

    def test_server_latency_comparison_omits_unrelated_message_report(self):
        self.save_message_timing_report()
        self.routing_approval()
        self.select_candidate(lambda row: "Server" in row["label"])
        question = "Compare the latency of the two server logs."
        quote = "Server latency was measured."
        captured = []

        def cite_log(records):
            captured.extend(records)
            return answer_from_quotes(records, [quote])

        for start, end in ((0, 3), (3, 5)):
            with self.subTest(server_records=end):
                for n in range(start, end):
                    self.text_source(f"Server log {n}", f"{quote} Sample {n}.")
                captured.clear()
                self.answer(self.submit(question), cite_log)
                self.assertEqual(len(captured), min(end, 4))
                self.assertTrue(all(quote in row["text"] for row in captured))
                if end == 3:
                    self.routing_http.assert_not_called()
                else:
                    self.routing_http.assert_called_once()
                    request = json.loads(self.routing_http.call_args.args[0].data)
                    candidates = request["input"][0]["content"][0]["data"]["candidates"]
                    self.assertEqual(len(candidates), end)
                    self.assertTrue(all(quote in row["preview"] for row in candidates))
                    captured.extend(candidates)
                payload = json.dumps(captured)
                self.assertNotIn("Median response interval", payload)
                self.assertNotIn("Mica", payload)

    def test_simple_and_previously_submitted_turns_skip_external_routing(self):
        logged = "Kestrel departure was logged at 08:00."
        for n in range(5):
            self.text_source(f"Departure account {n}", f"{logged} Ledger {n}.")
        complex_question = "Compare the Kestrel departure accounts."
        before_grant = self.submit(complex_question)
        self.routing_approval()
        old_grant = self.submit(complex_question)
        self.routing_approval("revoke")
        self.routing_approval()
        direct = self.submit("When was the Kestrel departure logged?")
        for turn in [before_grant, old_grant, direct]:
            with self.subTest(question=turn.question, turn=turn.pk):
                self.answer_http.reset_mock()
                result = self.answer(
                    turn, lambda records: answer_from_quotes(records, [logged])
                )
                self.assertEqual(result["claims"][0]["citations"][0]["quote"], logged)
                self.answer_http.assert_called_once()
        self.routing_http.assert_not_called()

    @override_settings(PANDECT_LOCAL_EMBEDDING=EMBEDDING)
    def test_approved_routing_keeps_an_indexed_match_without_shared_terms(self):
        quote = "The ferry moved out of port."
        ferry = self.text_source("Harbour log", quote)
        doc, _ = SearchDocument.objects.update_or_create(
            case=self.case,
            text=ferry,
            defaults={"complete": True, "profile": profile()},
        )
        SearchPassage.objects.create(
            case=self.case,
            document=doc,
            profile=doc.profile,
            start=0,
            length=len(quote),
            digest=sha256(quote.encode()).hexdigest(),
            embedding=UNIT,
        )
        CaseSearchIndex.objects.update_or_create(
            case=self.case,
            defaults={
                "status": "ready",
                "profile": doc.profile,
                "requested_by": self.owner,
            },
        )
        self.routing_approval()
        turn = self.submit("Compare accounts describing maritime departures.")
        with patch("core.case_search.semantic.embed", side_effect=vectors) as embed:
            result = self.answer(
                turn, lambda records: answer_from_quotes(records, [quote])
            )
        embed.assert_called_once()
        self.routing_http.assert_not_called()
        citation = result["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], quote)
        self.assertIn(str(ferry.pk), citation["url"])

    @override_settings(
        PANDECT_SEARCH_RERANK_PROVIDER="local", PANDECT_LOCAL_RERANK=LOCAL
    )
    def test_router_abstention_preserves_the_eligible_passage_reranker(self):
        question = "Compare the Kestrel departure accounts."
        for n in range(5):
            self.text_source(
                f"Kestrel departure account {n}",
                f"Kestrel departure log recorded movement from harbour at dawn. {n}.",
            )
        quote = "Kestrel log recorded movement from harbour at noon."
        witness = self.text_source("Wharf witness", quote)
        self.routing_approval()
        turn = self.submit(question)
        self.routing_http.return_value = response(
            {
                "model": "gpt-6-luna",
                "answers": [
                    {
                        "name": "next_source",
                        "type": "choice",
                        "choice": "keep",
                        "confidence": 0.95,
                    }
                ],
            }
        )

        def promote_witness(scope, query, texts, check, chat_turn):
            check()
            self.assertEqual(chat_turn.pk, turn.pk)
            witness_index = next(i for i, text in enumerate(texts) if quote in text)
            self.assertGreaterEqual(witness_index, 4)
            return sorted(range(len(texts)), key=lambda i: i != witness_index)

        with patch(
            "core.case_search.rerank._local_order", side_effect=promote_witness
        ) as rerank:
            result = self.answer(
                turn, lambda records: answer_from_quotes(records, [quote])
            )
        self.routing_http.assert_called_once()
        rerank.assert_called_once()
        citation = result["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], quote)
        self.assertIn(str(witness.pk), citation["url"])

    def test_explicit_full_review_uses_its_frozen_manifest_directly(self):
        for n in range(6):
            self.text_source(f"Kestrel account {n}", f"Departure log number {n}.")
        self.routing_approval()
        turn = self.submit(
            "Check every record for conflicting Kestrel departure accounts."
        )
        self.answer_http.return_value = response(
            openai_reply(decision("review", turn.question))
        )
        jobs.process_turn(turn.pk, turn.delivery_token)
        turn.refresh_from_db()
        self.assertEqual((turn.mode, turn.total), ("full", 6))
        self.routing_http.assert_not_called()
        self.answer_http.assert_called_once()

    def test_retrying_a_failed_answer_spends_only_one_routing_attempt(self):
        stamp = "Departure recorded at 08:00."
        for n in range(6):
            self.text_source(f"Kestrel departure {n}", f"{stamp} Register {n}.")
        self.routing_approval()
        self.select_candidate(lambda row: "departure 5" in row["label"])
        turn = self.submit("Compare the Kestrel departure registers.")
        self.answer_http.return_value = response(openai_reply({"invalid": "reply"}))
        jobs.process_turn(turn.pk, turn.delivery_token)
        turn.refresh_from_db()
        self.assertEqual(
            (turn.status, turn.error_code), ("failed", "model_result_structure")
        )
        services.retry_turn(self.users["editor"], self.case.pk, turn.chat_id, turn.pk)
        turn.refresh_from_db()
        result = self.answer(turn, lambda records: answer_from_quotes(records, [stamp]))
        self.routing_http.assert_called_once()
        self.assertEqual(self.answer_http.call_count, 2)
        self.assertEqual(result["claims"][0]["citations"][0]["quote"], stamp)

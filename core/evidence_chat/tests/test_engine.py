"""End-to-end worker checks with synthetic records and mocked model calls."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings

from cases.models import CaseMembership
from core.analysis.approval import approve_case_execution, revoke_case_execution
from core.analysis.errors import AnalysisError
from core.analysis.execution import configured_external_executions
from core.analysis.tests.test_execution import LOCAL
from core.analysis.tests.test_gateway import response
from core.analysis.tests.test_openai_gateway import OPENAI, openai_reply
from core.evidence_chat import ai, engine, jobs, services
from core.models import GatewayRequest
from core.testing import EvidenceViewTestMixin
from people.models import Person
from phone.models import PhoneCallEvidence
from text.models import TextEvidence


class QuestionRoutingTests(SimpleTestCase):
    def test_only_an_explicit_enumeration_overrides_an_ordinary_plan(self):
        for original, expected in (
            ("What total is printed on the receipt?", "answer"),
            ("Did the witness say “count me in”?", "answer"),
            ("Did the witness say “list every mention”?", "answer"),
            ("List every mention of pickup", "review"),
            ("Show me every match", "review"),
        ):
            with self.subTest(original=original):
                turn = SimpleNamespace(question=original)
                rewrite = {"action": "answer", "question": "List every mention"}
                with patch.object(engine, "_request", return_value=(rewrite, [])):
                    action, _, _ = engine.resolve_question(turn, None, [], [])
                self.assertEqual(action, expected)

    def test_clarification_can_resume_an_exhaustive_request(self):
        history = [
            {
                "question": "List every mention of the pickup.",
                "reply_context": "Which pickup?",
            }
        ]
        reply = {"action": "review", "question": "List every north gate pickup."}
        with patch.object(engine, "_request", return_value=(reply, [])) as request:
            action, _, _ = engine.resolve_question(
                SimpleNamespace(question="The north gate."), None, history, []
            )
        self.assertEqual(action, "review")
        self.assertEqual(request.call_args.args[3]["history"], history)


@override_settings(PANDECT_LOCAL_AI=LOCAL)
class ChatWorkerTests(EvidenceViewTestMixin, TestCase):
    def ask(self, question, **kwargs):
        return services.submit_turn(
            self.users["editor"],
            self.case.pk,
            question,
            submission_id=uuid4(),
            **kwargs,
        )

    def process(self, turn):
        jobs.process_turn(str(turn.pk), str(turn.delivery_token))
        turn.refresh_from_db()

    def finish(self, turn):
        for _ in range(32):
            if turn.status not in ("queued", "running"):
                break
            self.process(turn)
        self.assertEqual(turn.status, "complete", turn.error_code)

    def source(self, body, title="Pickup record"):
        return TextEvidence.objects.create(
            case=self.case, uploaded_by=self.users["owner"], title=title, body=body
        )

    def test_question_clarification_then_cited_conflict_and_current_followup(self):
        self.source("North gate: cancelled. Meet at the south gate.")
        self.source("Keep the north gate pickup.")
        first = self.ask("Which pickup was confirmed?")
        with patch.object(
            ai,
            "infer",
            return_value={"action": "ask", "question": "Which gate pickup?"},
        ):
            self.process(first)
        self.assertEqual(first.mode, "clarification")
        second = self.ask("The north gate", chat_id=first.chat_id)

        def infer(turn, authorize, prompt, schema):
            authorize()
            payload = json.loads(prompt.split("\n", 1)[1])
            if schema is engine.PLAN:
                self.assertEqual(
                    payload["history"][-1]["reply_context"], "Which gate pickup?"
                )
                return {
                    "action": "answer",
                    "question": "What do records say about the north gate pickup?",
                }
            claims = []
            for row in payload["records"]:
                quote = next(
                    (
                        q
                        for q in (
                            "North gate: cancelled.",
                            "Keep the north gate pickup.",
                        )
                        if q in row["text"]
                    ),
                    None,
                )
                if quote:
                    claims.append(
                        {
                            "text": quote,
                            "citations": [
                                {
                                    "id": row["id"],
                                    "quote": quote,
                                    "start": row["text"].index(quote),
                                }
                            ],
                        }
                    )
            return {
                "claims": claims,
                "lead": "Check the time of each account.",
                "clarification": "",
            }

        with patch.object(ai, "infer", side_effect=infer):
            self.process(second)
        self.assertEqual(second.status, "complete", second.error_code)
        answer = services.inspect_turn(second)
        self.assertEqual(len(answer["claims"]), 2)
        self.assertTrue(all(c["citations"][0]["url"] for c in answer["claims"]))
        self.assertIn("time", answer["lead"])

    def test_exhaustive_windows_overlap_and_failure_retries_without_duplicates(self):
        self.source(" " * 1300 + "pickup registered at Gate C. " + " " * 1300)
        turn = self.ask("List every mention of pickup")
        calls = 0

        def infer(turn, authorize, prompt, schema):
            nonlocal calls
            authorize()
            data = json.loads(prompt.split("\n", 1)[1])
            if schema is engine.PLAN:
                return {"action": "answer", "question": turn.question}
            calls += 1
            if calls == 2:
                raise AnalysisError("local_ai_busy")
            rows = []
            for row in data["records"]:
                quote = "pickup registered at Gate C."
                idx = row["text"].find(quote)
                refs = (
                    [{"id": row["id"], "quote": quote, "start": idx}]
                    if idx >= 0
                    else []
                )
                rows.append(
                    {
                        "id": row["id"],
                        "outcome": "match" if refs else "miss",
                        "citations": refs,
                    }
                )
            return {"records": rows}

        with patch.object(ai, "infer", side_effect=infer):
            self.process(turn)
            self.assertEqual(turn.mode, "full")
            old_token = str(turn.delivery_token)
            self.process(turn)
            jobs.process_turn(str(turn.pk), old_token)
            self.process(turn)
            self.assertEqual(
                (turn.status, turn.error_code), ("failed", "local_ai_busy")
            )
            turn = services.retry_turn(
                self.users["editor"], self.case.pk, turn.chat_id, turn.pk
            )
            self.finish(turn)
        self.assertEqual(turn.hits.count(), 1)
        self.assertEqual(turn.hits.get().start, 1300)
        self.assertEqual(turn.plan["matched_records"], 1)
        self.assertEqual(turn.total, turn.reviewed)

    def test_typed_date_and_participant_counts_with_missing_date(self):
        other = Person.objects.create(case=self.case, first_name="Adrian")
        for hour in (1, 19, None):
            PhoneCallEvidence.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                title="Synthetic call",
                sender=self.person,
                recipient=other,
                occurred_at=datetime(2024, 5, 4, hour, tzinfo=UTC) if hour else None,
            )
        with patch.object(
            ai, "infer", side_effect=AssertionError("deterministic count")
        ):
            from django.utils import timezone

            with timezone.override("America/Toronto"):
                turn = self.ask(
                    "How many calls between Adrian and Synthetic person on 2024-05-03?"
                )
            self.finish(turn)
            ambiguous = self.ask(
                "How many calls between Adrian and Synthetic person on Friday?"
            )
            self.finish(ambiguous)
        self.assertEqual(ambiguous.mode, "clarification")
        self.assertEqual(turn.plan["matched_records"], 1)
        self.assertEqual(turn.gaps, 1)
        self.assertEqual(turn.hits.count(), 1)

    def test_invalid_quotes_and_revoked_editor(self):
        self.source("Pickup at the north gate.")
        turn = self.ask("Where was the pickup?")
        with patch.object(
            ai,
            "infer",
            side_effect=[
                {"action": "answer", "question": turn.question},
                {
                    "clarification": "",
                    "lead": "",
                    "claims": [
                        {
                            "text": "Wrong",
                            "citations": [{"id": 0, "quote": "Wrong", "start": 0}],
                        }
                    ],
                },
            ],
        ):
            self.process(turn)
        self.assertEqual(turn.error_code, "model_quote_mismatch")
        self.assertFalse(turn.answer)
        turn = services.retry_turn(
            self.users["editor"], self.case.pk, turn.chat_id, turn.pk
        )
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.process(turn)
        self.assertEqual(turn.error_code, "access_removed")

    @override_settings(PANDECT_AI_GATEWAY=OPENAI)
    def test_external_chat_requires_separate_grant_and_tracks_turn_usage(self):
        self.source("The pickup moved to Friday.")
        self.case.ai_backend = "gateway"
        self.case.save(update_fields=["ai_backend"])
        routes = configured_external_executions(case=self.case)
        approve_case_execution(
            self.case,
            self.users["owner"],
            {key: value for key, value in routes.items() if key != "evidence_chat"},
        )
        with self.assertRaisesMessage(
            AnalysisError, "external_processing_not_approved"
        ):
            self.ask("When was the pickup?")
        approve_case_execution(self.case, self.users["owner"], routes)
        turn = self.ask("When was the pickup?")

        def first_reply(*args, **kwargs):
            revoke_case_execution(self.case)
            return response(
                openai_reply({"action": "answer", "question": turn.question})
            )

        with patch("core.analysis.gateway.build_opener") as build:
            build.return_value.open.side_effect = first_reply
            self.process(turn)
        self.assertEqual(turn.error_code, "external_processing_not_approved")
        self.assertEqual(build.return_value.open.call_count, 1)
        usage = GatewayRequest.objects.get(case=self.case, chat_turn=turn)
        self.assertEqual(usage.operation, "evidence_chat")

    def test_full_review_freezes_all_501_records_and_excludes_later_upload(self):
        import hashlib

        rows = [
            TextEvidence(
                case=self.case,
                uploaded_by=self.users["owner"],
                title=f"Record {i}",
                body="Synthetic testimony",
                sha256=hashlib.sha256(b"Synthetic testimony").hexdigest(),
            )
            for i in range(501)
        ]
        TextEvidence.objects.bulk_create(rows)
        turn = self.ask("How many text records are there?")
        self.process(turn)
        self.assertEqual(turn.total, 501)
        late = self.source("A later upload")
        self.finish(turn)
        self.assertEqual(turn.plan["matched_records"], 501)
        self.assertEqual(turn.hits.count(), 501)
        self.assertFalse(turn.sources.filter(evidence_id=late.pk).exists())

    def test_archived_context_cannot_leave_case_on_active_only_followup(self):
        from django.utils import timezone

        original = self.source("Unique synthetic source wording.")
        first = self.ask("When was the pickup?")
        with patch.object(
            ai,
            "infer",
            side_effect=[
                {"action": "answer", "question": first.question},
                {
                    "claims": [
                        {
                            "text": "Unique synthetic source wording.",
                            "citations": [
                                {
                                    "id": 0,
                                    "quote": "Unique synthetic source wording.",
                                    "start": 0,
                                }
                            ],
                        }
                    ],
                    "lead": "",
                    "clarification": "",
                },
            ],
        ):
            self.finish(first)
        original.is_archived = True
        original.archive_changed_at = timezone.now()
        original.archive_changed_by = self.users["owner"]
        original.save(
            update_fields=["is_archived", "archive_changed_at", "archive_changed_by"]
        )
        second = self.ask("Can you clarify?", chat_id=first.chat_id)

        def respond(turn, authorize, prompt, schema):
            authorize()
            self.assertNotIn("Unique synthetic source wording.", prompt)
            return {"action": "ask", "question": "Which source should I check?"}

        with patch.object(ai, "infer", side_effect=respond):
            self.finish(second)
        self.assertEqual(second.mode, "clarification")

    def test_partial_window_response_cannot_publish_as_full_coverage(self):
        self.source("Pickup at the south gate.")
        turn = self.ask("Find every pickup")
        with patch.object(
            ai,
            "infer",
            side_effect=[
                {"action": "review", "question": turn.question},
                {"records": []},
            ],
        ):
            self.process(turn)
            self.process(turn)
        self.assertEqual(turn.error_code, "model_result_structure")
        self.assertEqual(turn.reviewed, 0)
        self.assertFalse(turn.answer)

    def test_exhaustive_narrative_includes_cited_answer_and_complete_match_list(self):
        self.source("The north gate pickup was cancelled.")
        self.source("The south gate pickup was confirmed.")
        turn = self.ask("After checking every record, explain the pickup change.")

        def infer(turn, authorize, prompt, schema):
            authorize()
            data = json.loads(prompt.split("\n", 1)[1])
            if schema is engine.PLAN:
                return {"action": "review_answer", "question": turn.question}
            refs = []
            for row in data["records"]:
                quote = row["text"].split("\n", 1)[0]
                refs.append({"id": row["id"], "quote": quote, "start": 0})
            if schema is engine.REVIEW:
                return {
                    "records": [
                        {"id": ref["id"], "outcome": "match", "citations": [ref]}
                        for ref in refs
                    ]
                }
            return {
                "claims": [
                    {
                        "text": "One pickup was cancelled and one was confirmed.",
                        "citations": refs,
                    }
                ],
                "clarification": "",
                "lead": "",
            }

        with patch.object(ai, "infer", side_effect=infer):
            self.finish(turn)
        self.assertEqual(turn.plan["matched_records"], 2)
        self.assertEqual(len(services.inspect_turn(turn)["claims"][0]["citations"]), 2)
        self.assertIn("2 representative", turn.answer["summary"])

    def test_followup_to_full_review_uses_only_current_source_excerpts(self):
        from django.utils import timezone

        archived = self.source("Pickup moved to Blue Pier.", "Blue Pier dispatch")
        self.source("Pickup moved to Red Pier.", "Red Pier dispatch")
        first = self.ask("List every mention of pickup")

        def review(turn, authorize, prompt, schema):
            authorize()
            if schema is engine.PLAN:
                return {"action": "review", "question": turn.question}
            records = []
            for item in json.loads(prompt.split("\n", 1)[1])["records"]:
                quote = item["text"].split("\n", 1)[0]
                records.append(
                    {
                        "id": item["id"],
                        "outcome": "match",
                        "citations": [{"id": item["id"], "start": 0, "quote": quote}],
                    }
                )
            return {"records": records}

        with patch.object(ai, "infer", side_effect=review):
            self.finish(first)
        self.assertEqual(first.hits.count(), 2)
        archived.is_archived = True
        archived.archive_changed_at = timezone.now()
        archived.archive_changed_by = self.users["owner"]
        archived.save(
            update_fields=["is_archived", "archive_changed_at", "archive_changed_by"]
        )

        second = self.ask("Who sent the dispatch you found?", chat_id=first.chat_id)

        def followup(turn, authorize, prompt, schema):
            authorize()
            self.assertNotIn("Blue Pier", prompt)
            history = json.loads(prompt.split("\n", 1)[1])["history"][-1]
            self.assertIn("Red Pier dispatch", history["reply_context"])
            self.assertIn("Pickup moved to Red Pier.", history["reply_context"])
            return {"action": "ask", "question": "Which sender identifier?"}

        with patch.object(ai, "infer", side_effect=followup):
            self.finish(second)
        self.assertEqual(second.mode, "clarification")

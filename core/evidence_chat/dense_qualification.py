"""Opt-in 100,000-record case with thousands of genuinely relevant messages."""

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from config.settings import PANDECT_LOCAL_AI
from core.analysis.execution import configured_external_executions
from core.analysis.testing import approve_case_execution
from core.analysis.tests.test_openai_gateway import OPENAI
from core.evidence_chat import ai, jobs, services
from core.evidence_chat.qualification import claim_supports, database_counters
from core.evidence_chat.tests.test_decisions_worker import answer_from_quotes
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from people.models import Person
from phone.models import TextMessageEvidence

CASES = (("Aster", 10), ("Juniper", 1000), ("Cobalt", 20000))
START = datetime(2026, 1, 1, tzinfo=UTC)


@skipUnless(
    os.getenv("PANDECT_QUALIFY_DENSE_CHAT") == "1", "Explicit scale qualification"
)
@override_settings(PANDECT_AI_GATEWAY=OPENAI)
class DenseCaseQualification(EvidenceViewTestMixin, TestCase):
    def test_known_answers_with_many_relevant_records(self):
        owner = self.users["owner"]
        witness = Person.objects.create(case=self.case, first_name="Witness")
        live = os.getenv("PANDECT_QUALIFY_DENSE_CHAT_LIVE") == "1"
        if live:
            self.assertIn(
                urlsplit(PANDECT_LOCAL_AI["text"]["url"]).hostname,
                {"localhost", "127.0.0.1", "::1"},
            )
            self.enterContext(self.settings(PANDECT_LOCAL_AI=PANDECT_LOCAL_AI))
        self.case.ai_backend = "local" if live else "gateway"
        self.case.save(update_fields=["ai_backend"])
        if not live:
            approve_case_execution(
                self.case, owner, configured_external_executions(case=self.case)
            )
        infer = ai.infer
        rows = []
        rare = {}
        index = 0
        for topic, count in (*CASES, ("Inventory", 100000 - sum(n for _, n in CASES))):
            for offset in range(count):
                index += 1
                exception = offset == count - 1
                item = TextMessageEvidence(
                    pk=UUID(int=index),
                    case=self.case,
                    uploaded_by=owner,
                    sender=witness if exception else self.person,
                    recipient=self.person,
                    title="Register",
                    body=(
                        f"{topic} observer recorded arrival at 08:10."
                        if exception
                        else ("Fwd: " if offset else "")
                        + f"{topic} dispatch reported arrival at 09:25."
                    ),
                    occurred_at=None
                    if offset == 1
                    else START + timedelta(days=(2 if exception else offset % 7)),
                )
                item.sha256 = sha256(item.original_bytes()).hexdigest()
                rows.append(item)
                if exception:
                    rare[topic] = item.pk
                if len(rows) == 500:
                    TextMessageEvidence.objects.bulk_create(rows)
                    rows = []
        self.assertFalse(rows)
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE phone_textmessageevidence")

        for topic, count in CASES:
            with self.subTest(topic=topic):
                self.exercise_topic(topic, count, rare[topic], infer if live else None)
        self.assertFalse(AnalysisRun.objects.exists())

    def exercise_topic(self, topic, count, rare, infer):
        def answer(turn, authorize, prompt, schema):
            nonlocal prompt_bytes
            prompt_bytes += len(prompt.encode())
            if infer:
                return infer(turn, authorize, prompt, schema)
            authorize()
            records = json.loads(prompt.split("\n", 1)[1])["records"]
            return answer_from_quotes(
                records,
                [
                    f"{topic} observer recorded arrival at 08:10.",
                    f"{topic} dispatch reported arrival at 09:25.",
                ],
            )

        prompt_bytes = 0
        with patch.object(ai, "infer", side_effect=answer) as model:
            ordinary = self.case_question(
                f"Compare the {topic} arrival accounts.",
                topic,
                "interpret",
                rare,
                model,
                lambda: prompt_bytes,
            )
            selection = self.case_question(
                f'Who received "{topic}"?',
                topic,
                "count",
                count,
                model,
                lambda: prompt_bytes,
            )

            if count == 20000:
                from core.evidence_chat.deeper_qualification import (
                    qualify_deeper_review,
                )

                typed = self.case_question(
                    f"Compare the {topic} arrival accounts type:text-message",
                    topic,
                    "interpret",
                    rare,
                    model,
                    lambda: prompt_bytes,
                )
                qualify_deeper_review(self, ordinary, typed, selection, count)

    def case_question(self, question, topic, route, expected, model, byte_count):
        before = database_counters()
        calls, sent = model.call_count, byte_count()
        start = monotonic()
        with CaptureQueriesContext(connection) as queries:
            turn = services.submit_turn(
                self.users["owner"], self.case.pk, question, submission_id=uuid4()
            )
            for _ in range(4):
                jobs.process_turn(turn.pk, turn.delivery_token)
                turn.refresh_from_db()
                if turn.status not in ("queued", "running"):
                    break
            reply = services.inspect_turn(turn)
        elapsed = monotonic() - start
        after = database_counters()
        metrics = {
            "topic": topic,
            "route": route,
            "seconds": round(elapsed, 3),
            "queries": len(queries),
            "model_calls": model.call_count - calls,
            "prompt_bytes": byte_count() - sent,
            "reported_tokens": sum(
                entry.get("input_tokens", 0) + entry.get("output_tokens", 0)
                for entry in turn.model_usage
            )
            if turn.model_usage
            else None,
            **{key: value - before[key] for key, value in after.items()},
        }
        print("DENSE_CHAT", json.dumps(metrics), flush=True)
        self.assertEqual(turn.status, "complete", turn.error_code)
        self.assertIsNotNone(reply)
        if route == "interpret":
            for claim in reply["claims"]:
                for citation in claim["citations"]:
                    self.assertIn(topic, citation["quote"], claim)
            for clock, person in (("08:10", "observer"), ("09:25", "dispatch")):
                self.assertTrue(
                    any(
                        claim_supports(claim["text"], clock, person)
                        and any(
                            clock in ref["quote"]
                            and (clock != "08:10" or str(expected) in ref["url"])
                            for ref in claim["citations"]
                        )
                        for claim in reply["claims"]
                    ),
                    reply,
                )
            self.assertLessEqual(metrics["prompt_bytes"], 20000)
            self.assertEqual(metrics["model_calls"], 1)
        else:
            self.assertEqual(
                turn.plan["recorded_selection"]["membership"]["count"], expected
            )
            coverage = turn.plan["recorded_selection"]["coverage"]
            self.assertEqual(coverage["matched"], expected)
            self.assertEqual(coverage["unknown_time"], 1)
            self.assertEqual(coverage["forwarded"], expected - 2)
            self.assertEqual(metrics["model_calls"], 0)
        return turn

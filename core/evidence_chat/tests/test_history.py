"""Selective conversation recall using stored turns and current evidence."""

import json
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from core.analysis.tests.test_execution import LOCAL
from core.evidence_chat import ai, engine, jobs, services
from core.evidence_chat.tests.test_engine import decision
from core.models import ChatTurn, EvidenceChat
from core.testing import EvidenceViewTestMixin
from text.models import TextEvidence


@override_settings(PANDECT_LOCAL_AI=LOCAL)
class ConversationRecallTests(EvidenceViewTestMixin, TestCase):
    def ask(self, question, chat=None):
        return services.submit_turn(
            self.users["editor"],
            self.case.pk,
            question,
            submission_id=uuid4(),
            chat_id=chat,
        )

    def finish(self, turn):
        jobs.process_turn(turn.pk, turn.delivery_token)
        turn.refresh_from_db()

    def saved_turn(self, chat, ordinal, question):
        return ChatTurn.objects.create(
            case=chat.case,
            chat=chat,
            asked_by=chat.created_by,
            ordinal=ordinal,
            question=question,
            status="complete",
            mode="clarification",
            submission_id=uuid4(),
            answer=engine.empty_answer(clarification="Which entry?"),
        )

    def discussion(self):
        quote = "Fallow arrived at berth 17."
        item = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["owner"],
            title="Wharf register",
            body=quote,
        )
        older = self.ask("What did the wharf register record?")

        def answer(active, authorize, prompt, schema):
            authorize()
            row = next(
                r
                for r in json.loads(prompt.split("\n", 1)[1])["records"]
                if quote in r["text"]
            )
            return decision(
                "answer",
                claims=[
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
                ],
                lead="Check whether unloading was recorded.",
            )

        with patch.object(ai, "infer", side_effect=answer):
            self.finish(older)
        self.assertEqual(older.status, "complete", older.error_code)
        # These earlier turns are already saved, as in chats predating the index.
        for n, question in enumerate(("Lyle", "Pine", "Bicycle", "Compact discs"), 2):
            self.saved_turn(older.chat, n, question)
        return item, older

    def test_recall_matches_an_older_claim_and_returns_to_its_original(self):
        item, old = self.discussion()
        other = EvidenceChat.objects.create(
            case=self.case, created_by=self.users["owner"]
        )
        self.saved_turn(other, 1, "Fallow Fallow: unrelated private question")
        turn = self.ask(
            "Can you remind me again what we previously said in our earlier "
            "conversation when we first discussed Fallow?",
            old.chat_id,
        )

        def answer(active, authorize, prompt, schema):
            authorize()
            payload = json.loads(prompt.split("\n", 1)[1])
            context = payload["history"]
            self.assertEqual(len(context), 3)
            self.assertEqual(context[0]["earlier_turn"], 1)
            self.assertIn("berth 17", context[0]["reply_context"])
            self.assertIn("whether unloading was recorded", context[0]["reply_context"])
            self.assertNotIn("unrelated private question", prompt)
            self.assertNotIn("Pine", prompt)
            self.assertTrue(
                any("Fallow arrived" in row["text"] for row in payload["records"])
            )
            return decision("ask", "Which event after docking?")

        with (
            patch.object(ai, "infer", side_effect=answer),
            patch.object(jobs, "_rank", wraps=jobs._rank) as rank,
        ):
            self.finish(turn)
        self.assertEqual(turn.status, "complete", turn.error_code)
        hints = rank.call_args.args[3]
        self.assertEqual(hints[0][0]["evidence_id"], str(item.pk))
        self.assertEqual(turn.plan["recalled_turn"], str(old.pk))
        self.assertEqual(len(turn.plan["recalled_sources"]), 1)

    def test_archive_blocks_recalled_context_in_flight_and_after_publication(self):
        item, old = self.discussion()
        published = self.ask("Return to answer #1", old.chat_id)
        # A clarification may use the older topic even if none of its evidence
        # is selected in this turn's own shortlist.
        with (
            patch.object(jobs, "_rank", return_value=[]),
            patch.object(ai, "infer", return_value=decision("ask", "Which entry?")),
        ):
            self.finish(published)
        self.assertEqual(published.status, "complete")
        self.assertFalse(published.sources.exists())
        self.assertIsNotNone(services.inspect_turn(published))
        continuation = self.ask("What happened next?", old.chat_id)
        with (
            patch.object(jobs, "_rank", return_value=[]),
            patch.object(
                ai, "infer", return_value=decision("ask", "Which subsequent event?")
            ),
        ):
            self.finish(continuation)
        self.assertEqual(continuation.status, "complete")
        self.assertEqual(
            continuation.plan["recalled_sources"], published.plan["recalled_sources"]
        )
        active = self.ask("Return to answer #1", old.chat_id)

        def archive_then_authorize(turn, authorize, prompt, schema):
            self.assertIn("berth 17", prompt)
            item.is_archived = True
            item.archive_changed_at = timezone.now()
            item.archive_changed_by = self.users["owner"]
            item.save(
                update_fields=[
                    "is_archived",
                    "archive_changed_at",
                    "archive_changed_by",
                ]
            )
            authorize()
            self.fail("Stale memory reached inference")

        with (
            patch.object(jobs, "_rank", return_value=[]),
            patch.object(ai, "infer", side_effect=archive_then_authorize),
        ):
            self.finish(active)
        self.assertEqual(
            (active.status, active.error_code), ("failed", "source_changed")
        )
        self.assertIsNone(services.inspect_turn(published))
        self.assertIsNone(services.inspect_turn(continuation))

    def test_short_followup_and_foreign_pins_retain_chat_boundaries(self):
        item, old = self.discussion()
        latest = self.ask("What happened then?", old.chat_id)
        with patch.object(ai, "infer", return_value=decision("ask", "Which event?")):
            self.finish(latest)
        self.assertEqual(latest.status, "complete")
        self.assertNotIn("recalled_turn", latest.plan)
        self.assertEqual(len(latest.plan["history_turns"]), 3)
        foreign = EvidenceChat.objects.create(
            case=self.other_case, created_by=self.users["outsider"]
        )
        unrelated = self.saved_turn(foreign, 1, "Fallow confidential")
        latest.plan["history_turns"] = [str(unrelated.pk)]
        self.assertEqual(jobs._history(latest), [])
        unrelated.plan["recalled_sources"] = [
            str(old.sources.get(evidence_id=item.pk).pk)
        ]
        self.assertIsNone(services.inspect_turn(unrelated))

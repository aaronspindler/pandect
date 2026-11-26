"""Approved scope, cost, checkpoint recovery and current access for deeper reviews."""

import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis import chat_usage
from core.analysis.errors import AnalysisError
from core.analysis.execution import snapshot_execution
from core.analysis.gateway import GatewayClient
from core.analysis.tests.test_execution import LOCAL
from core.analysis.tests.test_gateway import CONFIG, error
from core.evidence.retention import set_evidence_archive
from core.evidence_chat import ai, deep_review, engine, jobs, services
from core.evidence_chat.tests.test_decisions_worker import answer_from_quotes
from core.evidence_chat.tests.test_engine import decision
from core.models import ChatTurn
from core.testing import EvidenceViewTestMixin
from phone.models import TextMessageEvidence
from text.models import TextEvidence


@override_settings(PANDECT_LOCAL_AI=LOCAL)
class ApprovedReviewTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner, self.editor = self.users["owner"], self.users["editor"]
        self.items = [
            TextEvidence.objects.create(
                case=self.case,
                uploaded_by=self.owner,
                title=f"Receipt {n}",
                body=f"Marigold docket {n} omitted its stamp.",
            )
            for n in range(12)
        ]
        self.other = TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Unrelated",
            body="A blue umbrella was returned.",
        )
        self.question = "What happened with Marigold?"
        self.parent = services.submit_turn(
            self.editor, self.case.pk, self.question, submission_id=uuid4()
        )
        with patch.object(ai, "infer", side_effect=self.infer):
            self.process(self.parent)
        self.assertEqual(self.parent.status, "complete", self.parent.error_code)
        self.url = reverse(
            "core:chat:deep_review",
            args=(self.case.pk, self.parent.chat_id, self.parent.pk),
        )

    def infer(self, turn, authorize, prompt, schema):
        authorize()
        records = json.loads(prompt.split("\n", 1)[1])["records"]
        if turn.review_of_id:
            chat_usage.reserve(turn, "answer", len(prompt), output_limit=1024)
        if schema is engine.REVIEW:
            self.assertTrue(all("umbrella" not in w["text"] for w in records))
            return {
                "records": [
                    {
                        "id": r["id"],
                        "outcome": "match",
                        "citations": [
                            {
                                "id": r["id"],
                                "quote": "Marigold",
                                "start": r["text"].index("Marigold"),
                            }
                        ],
                    }
                    for r in records
                ]
            }
        if schema is engine.ANSWER:
            return answer_from_quotes(records, ["Marigold"])
        return decision(
            "answer",
            lead="Inspect the other original dockets.",
            claims=[
                {
                    "text": "A docket mentions Marigold.",
                    "citations": [
                        {
                            "id": 0,
                            "quote": "Marigold",
                            "start": records[0]["text"].index("Marigold"),
                        }
                    ],
                }
            ],
        )

    def process(self, turn):
        jobs.process_turn(turn.pk, turn.delivery_token)
        turn.refresh_from_db()

    def finish(self, turn):
        with patch.object(ai, "infer", side_effect=self.infer):
            for _ in range(20):
                if turn.status not in ("queued", "running"):
                    break
                self.process(turn)
        self.assertEqual(turn.status, "complete", turn.error_code)

    def approve(self, limit=None):
        scope, _, token = deep_review.estimate(self.parent, self.owner)
        return deep_review.approve(
            self.owner,
            self.case.pk,
            self.parent.chat_id,
            self.parent.pk,
            self.question,
            token,
            limit or scope["limit"],
        )

    def test_signed_scope_approval_review_and_checkpoint_recovery(self):
        original = self.parent.answer.copy()
        self.login("owner")
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "12 candidate source entries")
        self.assertContains(page, "about 4 generation requests")
        self.assertContains(page, "configured private model")
        self.assertFalse(ChatTurn.objects.filter(review_of=self.parent).exists())
        form = {
            "action": "approve",
            "confirmation": page.context["form"].initial["confirmation"],
            "question": "Which Marigold dockets omit their stamp?",
            "request_limit": 10,
        }
        self.assertEqual(self.client.post(self.url, form).status_code, 302)
        self.assertEqual(self.client.post(self.url, form).status_code, 302)
        child = ChatTurn.objects.get(review_of=self.parent)
        self.process(child)
        self.assertEqual(child.total, len(self.items))
        self.assertEqual(
            set(child.sources.values_list("evidence_id", flat=True)),
            {i.pk for i in self.items},
        )
        with patch.object(ai, "infer", side_effect=self.infer):
            self.process(child)
        self.assertEqual(child.reviewed, 4)
        old_token = child.delivery_token
        services.recover_interrupted_delivery(child.pk, old_token)
        child.refresh_from_db()
        jobs.process_turn(child.pk, old_token)
        self.finish(child)
        self.assertEqual(
            (child.reviewed, child.hits.count(), child.review_requests), (12, 12, 4)
        )
        self.assertIsNotNone(services.inspect_turn(child))
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.answer, original)

    def test_cost_cap_survives_retries_and_discloses_partial_result(self):
        child = self.approve(limit=1)
        self.finish(child)
        self.assertEqual((child.reviewed, child.gaps, child.review_requests), (4, 8, 1))
        self.assertEqual(child.hits.count(), 4)
        self.assertIn("approved generation limit", child.answer["summary"])
        self.assertIsNotNone(services.inspect_turn(child))

    def test_stale_offer_changed_destination_revocation_and_case_access(self):
        scope, _, token = deep_review.estimate(self.parent, self.owner)
        set_evidence_archive(
            self.owner, self.case.pk, "text", self.items[-1].pk, archived=True
        )
        with self.assertRaises((AnalysisError, ValidationError)):
            deep_review.approve(
                self.owner,
                self.case.pk,
                self.parent.chat_id,
                self.parent.pk,
                self.question,
                token,
                scope["limit"],
            )
        self.assertFalse(ChatTurn.objects.filter(review_of=self.parent).exists())
        for role in ("viewer", "outsider"):
            self.login(role)
            self.assertIn(self.client.get(self.url).status_code, (403, 404))
        self.login("owner")
        with patch.object(
            ai, "turn_execution", return_value=({"backend": "different"}, "new")
        ):
            self.assertContains(self.client.get(self.url), "settings", status_code=200)
        set_evidence_archive(
            self.owner, self.case.pk, "text", self.items[-1].pk, archived=False
        )
        child = self.approve()
        self.process(child)
        CaseMembership.objects.filter(case=self.case, user=self.editor).delete()
        with patch.object(ai, "infer") as model:
            self.process(child)
        self.assertEqual(child.error_code, "access_removed")
        model.assert_not_called()

    def test_scoped_review_reuses_the_entire_exact_selection(self):
        parties = {
            "case": self.case,
            "uploaded_by": self.editor,
            "sender": self.person,
            "recipient": self.person,
        }
        matching = [
            TextMessageEvidence.objects.create(
                **parties, title="Recorded message", body="Freight docket sealed."
            )
            for _ in range(17)
        ]
        TextMessageEvidence.objects.create(
            **parties, title="Unrelated", body="Blue umbrella."
        )
        parent = services.submit_turn(
            self.editor,
            self.case.pk,
            'Who received "Freight docket"?',
            submission_id=uuid4(),
        )
        self.process(parent)
        self.assertEqual(parent.status, "complete", parent.error_code)
        self.assertTrue(parent.plan["recorded_selection"]["snapshot"])
        scope, _, token = deep_review.estimate(parent, self.owner)
        self.assertEqual(scope["count"], 17)
        child = deep_review.approve(
            self.owner,
            self.case.pk,
            parent.chat_id,
            parent.pk,
            "What do the selected messages say about sealing?",
            token,
            scope["limit"],
        )
        self.process(child)
        self.assertEqual(
            set(child.sources.values_list("evidence_id", flat=True)),
            {x.pk for x in matching},
        )
        self.assertEqual(child.total, 17)

    def test_interpretive_filters_do_not_authorize_unrelated_selected_originals(self):
        def message(body, day):
            return TextMessageEvidence.objects.create(
                case=self.case,
                uploaded_by=self.editor,
                sender=self.person,
                recipient=self.person,
                title="Register",
                body=body,
                occurred_at=datetime(2026, 4, day, 12, tzinfo=UTC) if day else None,
            )

        match = message("Gate Seven received Marigold.", 3)
        roses = message("Gate Seven received roses.", 3)
        wrong_phrase = message("Gate Nine received Marigold.", 3)
        wrong_day = message("Gate Seven received Marigold.", 2)
        unknown = message("Gate Seven received Marigold.", None)
        self.login("owner")
        examples = (
            (
                "What about Marigold type:text-message?",
                [match, wrong_phrase, wrong_day, unknown],
            ),
            (
                'What do messages containing "Gate Seven" say about Marigold?',
                [match, wrong_day, unknown],
            ),
            (
                'Why do messages containing "Gate Seven" differ?',
                [match, roses, wrong_day, unknown],
            ),
            (
                "What about Marigold type:text-message on:2026-04-03?",
                [match, wrong_phrase],
            ),
            (
                "What about Marigold type:text-message on 2026-04-03?",
                [match, wrong_phrase],
            ),
        )
        for question, expected in examples:
            with self.subTest(question=question):
                parent = services.submit_turn(
                    self.editor, self.case.pk, question, submission_id=uuid4()
                )
                with patch.object(ai, "infer", return_value=decision("answer")):
                    self.process(parent)
                self.assertEqual(parent.status, "complete", parent.error_code)
                selection = parent.plan["recorded_selection"]
                self.assertTrue(selection["context_only"])
                if question.startswith("Why"):
                    self.assertEqual(selection["membership"]["count"], len(expected))
                else:
                    self.assertGreater(selection["membership"]["count"], len(expected))
                _, _, token = deep_review.estimate(parent, self.owner)
                child = deep_review.approve(
                    self.owner,
                    self.case.pk,
                    parent.chat_id,
                    parent.pk,
                    question,
                    token,
                    1,
                )
                self.process(child)
                self.assertEqual(child.total, len(expected), child.error_code)
                self.assertEqual(
                    set(child.sources.values_list("evidence_id", flat=True)),
                    {row.pk for row in expected},
                )

    @override_settings(PANDECT_AI_GATEWAY=CONFIG)
    def test_transport_retry_cannot_exceed_the_single_approved_request(self):
        child = self.approve(limit=1)
        child.execution = snapshot_execution(
            "evidence_chat", {"backend": "gateway"}, case=self.case
        )
        child.status = "running"
        child.save(update_fields=("execution", "status"))
        gateway = GatewayClient(None, lambda **kwargs: self.case, chat_turn=child)
        with (
            patch("core.analysis.gateway.build_opener") as transport,
            patch("core.analysis.gateway.time.sleep"),
        ):
            transport.return_value.open.side_effect = error(503)
            with self.assertRaisesMessage(AnalysisError, "chat_deeper_review_limit"):
                gateway.infer("Review selected synthetic records.", {})
        self.assertEqual(transport.return_value.open.call_count, 1)
        child.refresh_from_db()
        self.assertEqual(child.review_requests, 1)

    def test_approved_extension_resumes_at_unfinished_source_cursor(self):
        TextEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Zircon",
            body="Marigold flower. " * 400,
        )
        parent = services.submit_turn(
            self.editor, self.case.pk, "What about Zircon?", submission_id=uuid4()
        )
        with patch.object(ai, "infer", side_effect=self.infer):
            self.process(parent)
        _, expected, token = deep_review.estimate(parent, self.owner)
        self.assertGreater(expected, 2)
        partial = deep_review.approve(
            self.owner,
            self.case.pk,
            parent.chat_id,
            parent.pk,
            parent.question,
            token,
            1,
        )
        self.finish(partial)
        self.assertEqual(partial.plan["unreviewed"], 1)
        previous = partial.sources.get()
        self.assertGreater(previous.cursor, 0)
        self.assertGreater(partial.hits.count(), 0)
        self.assertTrue(deep_review.offer(partial))
        scope, _, next_token = deep_review.estimate(partial, self.owner)
        self.assertTrue(scope["resume"])
        with self.assertRaises(ValidationError):
            deep_review.approve(
                self.owner,
                self.case.pk,
                parent.chat_id,
                partial.pk,
                "An unrelated question",
                next_token,
                6,
            )
        continued = deep_review.approve(
            self.owner,
            self.case.pk,
            parent.chat_id,
            partial.pk,
            parent.question,
            next_token,
            6,
        )
        self.process(continued)
        self.assertEqual(continued.sources.get().cursor, previous.cursor)
        self.finish(continued)
        self.assertEqual(continued.reviewed, 1)
        self.assertTrue(
            all(
                start >= previous.cursor
                for start in continued.hits.values_list("start", flat=True)
            )
        )
        partial.refresh_from_db()
        self.assertEqual((partial.plan["unreviewed"], partial.review_requests), (1, 1))

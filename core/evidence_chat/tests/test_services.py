from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import Http404
from django.test import TestCase

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.evidence.deletion import plan_deletion
from core.evidence_chat import corpus, services
from core.models import ChatMatch, ChatSource, ChatTurn, ImportantItem, ImportantNote
from core.tests.testing import EvidenceViewTestMixin


class ChatServiceTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        route = patch(
            "core.evidence_chat.ai.turn_execution",
            return_value=({"location": "external", "backend": "gateway"}, "synthetic"),
        )
        route.start()
        self.addCleanup(route.stop)
        queue = patch("core.evidence_chat.checkpoints.enqueue_turn")
        self.enqueue = queue.start()
        self.addCleanup(queue.stop)

    def ask(self, role="owner", question="When was the pickup?", **kwargs):
        kwargs.setdefault("submission_id", uuid4())
        return services.submit_turn(self.users[role], self.case.pk, question, **kwargs)

    def test_submissions_keep_case_route_and_question_identity(self):
        key = uuid4()
        first = self.ask(submission_id=key)
        duplicate = self.ask(submission_id=key)
        self.assertEqual(first.pk, duplicate.pk)
        self.enqueue.assert_called_once()
        for role, error in (("viewer", PermissionDenied), ("outsider", Http404)):
            with self.assertRaises(error):
                self.ask(role)
        with self.assertRaises(ValidationError):
            self.ask(submission_id=key, question="Different question")
        with self.assertRaises(ValidationError):
            self.ask(chat_id=first.chat_id)
        with self.assertRaises(AnalysisError):
            self.ask(expected_route=({"location": "external"}, "changed"))
        with self.assertRaises(Http404):
            services.submit_turn(
                self.users["outsider"],
                self.other_case.pk,
                "Other case question?",
                chat_id=first.chat_id,
                submission_id=uuid4(),
            )
        first.status = "complete"
        first.save(update_fields=["status"])
        following = self.ask("editor", chat_id=first.chat_id)
        self.assertEqual(following.ordinal, 2)
        self.assertFalse(following.include_archived)

    def test_same_key_has_an_independent_turn_for_each_user_and_case(self):
        key = uuid4()
        first = self.ask(submission_id=key)
        editor = self.ask("editor", submission_id=key)
        CaseMembership.objects.create(
            case=self.other_case, user=self.users["editor"], role="editor"
        )
        other_case = services.submit_turn(
            self.users["editor"],
            self.other_case.pk,
            "Other case question?",
            submission_id=key,
        )
        self.assertEqual(len({first.pk, editor.pk, other_case.pk}), 3)
        self.assertEqual(self.ask("editor", submission_id=key).pk, editor.pk)
        self.assertEqual(self.enqueue.call_count, 3)

    def test_legacy_archived_question_cannot_resume_processing(self):
        turn = self.ask()
        turn.include_archived = True
        turn.status = ChatTurn.Status.FAILED
        turn.save(update_fields=["include_archived", "status"])

        duplicate = self.ask(submission_id=turn.submission_id)
        self.assertEqual(duplicate.pk, turn.pk)
        self.assertTrue(duplicate.include_archived)
        self.enqueue.assert_called_once()

        with self.assertRaisesMessage(ValidationError, "active evidence"):
            services.retry_turn(
                self.users["owner"], self.case.pk, turn.chat_id, turn.pk
            )
        turn.refresh_from_db()
        self.assertEqual(turn.status, ChatTurn.Status.FAILED)
        self.enqueue.assert_called_once()

    def test_source_deletion_inventory_includes_chat_followups_marks_and_notes(self):
        item = self.text()
        first = self.ask()
        locator = next(corpus.discover(self.case, False))
        source = ChatSource.objects.create(
            case=self.case,
            turn=first,
            key=locator["key"],
            locator=locator,
            record_key=locator["record_key"],
            evidence_kind="text",
            evidence_id=item.pk,
        )
        citation = {"key": source.key, "start": 0, "end": 8, "quote": "Original"}
        first.answer = {
            "claims": [
                {"text": "The record starts with 'Original'.", "citations": [citation]}
            ]
        }
        first.status = "complete"
        first.save(update_fields=["answer", "status"])
        self.assertEqual(
            services.inspect_turn(first)["claims"][0]["citations"][0]["quote"],
            "Original",
        )
        match = ChatMatch.objects.create(
            case=self.case, turn=first, source=source, start=0, end=8, quote="Original"
        )
        second = self.ask(chat_id=first.chat_id)
        saved = ImportantItem.objects.create(case=self.case, chat_turn=first)
        note = ImportantNote.objects.create(
            item=saved, author=self.users["owner"], body="Check the time."
        )
        plan = plan_deletion(item, "text")
        self.assertFalse(plan.blocker)
        ids = {row["id"] for row in plan.entries}
        self.assertTrue(
            {
                str(obj.pk)
                for obj in (first, second, first.chat, source, match, saved, note)
            }
            <= ids
        )
        source.locator = locator | {"version": "changed"}
        source.save(update_fields=["locator"])
        self.assertIsNone(services.inspect_turn(first))

    def test_historical_total_withholds_sources_moved_out_of_case(self):
        item = self.text()
        turn = self.ask()
        locator = next(corpus.discover(self.case, False))
        ChatSource.objects.create(
            case=self.case,
            turn=turn,
            key=locator["key"],
            locator=locator,
            record_key=locator["record_key"],
            evidence_kind="text",
            evidence_id=item.pk,
            status="match",
        )
        turn.mode = "full"
        turn.status = "complete"
        turn.answer = {"summary": "One matching record."}
        turn.save(update_fields=["mode", "status", "answer"])
        self.assertTrue(services.reply_available(turn))
        type(item).objects.filter(pk=item.pk).update(case=self.other_case)
        self.assertFalse(services.reply_available(turn))

    def test_interrupted_delivery_requires_current_token_and_eligible_requester(self):
        turn = self.ask()
        with self.assertRaisesMessage(CommandError, "its token changed"):
            call_command(
                "recover_evidence_chat",
                str(turn.pk),
                "--confirm-interrupted",
                str(uuid4()),
            )
        with patch("core.evidence_chat.ai.check_execution"):
            call_command(
                "recover_evidence_chat",
                str(turn.pk),
                "--confirm-interrupted",
                str(turn.delivery_token),
                stdout=StringIO(),
            )
        old = turn.delivery_token
        turn.refresh_from_db()
        self.assertNotEqual(old, turn.delivery_token)
        turn.status = "failed"
        turn.save(update_fields=["status"])
        self.assertEqual(
            services.retry_turn(
                self.users["editor"], self.case.pk, turn.chat_id, turn.pk
            ).status,
            ChatTurn.Status.QUEUED,
        )

        revoked = self.ask("editor")
        CaseMembership.objects.filter(case=self.case, user=revoked.asked_by).delete()
        self.enqueue.reset_mock()
        with self.assertRaisesMessage(CommandError, "no longer authorized"):
            call_command(
                "recover_evidence_chat",
                str(revoked.pk),
                "--confirm-interrupted",
                str(revoked.delivery_token),
            )
        self.enqueue.assert_not_called()

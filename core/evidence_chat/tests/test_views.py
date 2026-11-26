"""Case and source access for the shared chat and Important readers."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.evidence.retention import set_evidence_archive
from core.evidence_chat import ai, corpus, decisions_approval, jobs
from core.models import ChatSource, ChatTurn, EvidenceChat, ImportantItem
from core.testing import EvidenceViewTestMixin

LOCAL_AI = {
    "text": {
        "model": "qwen3.5:9b",
        "url": "http://127.0.0.1:11434",
        "runtime": "ollama@0.40.11",
        "sha256": "a" * 64,
        "context_tokens": 16384,
    },
    "timeout": 10,
    "max_output_tokens": 512,
    "max_request_bytes": 64000,
    "max_response_bytes": 64000,
}


@override_settings(PANDECT_LOCAL_AI=LOCAL_AI)
class ChatViewTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.login("editor")
        self.index = reverse("core:chat:list", args=[self.case.pk])
        self.form_data = {
            "question": "When did the pickup move?",
            "route": self.client.get(self.index).context["route_token"],
            "submission_id": uuid4(),
        }

    def _ask(self):
        self.assertEqual(self.client.post(self.index, self.form_data).status_code, 302)
        return ChatTurn.objects.get(submission_id=self.form_data["submission_id"])

    def test_case_chat_is_shared_current_and_duplicate_safe(self):
        turn = self._ask()
        self._ask()
        self.assertEqual(ChatTurn.objects.count(), 1)
        url = reverse("core:chat:detail", args=[self.case.pk, turn.chat_id])
        self.login("viewer")
        self.assertContains(self.client.get(self.index), turn.question)
        self.assertContains(self.client.get(url), "Queued")
        self.assertNotContains(self.client.get(url), "Ask follow-up")
        self.assertEqual(self.client.post(url, self.form_data).status_code, 403)
        self.login("owner")
        foreign = reverse("core:chat:detail", args=[self.case.pk, uuid4()])
        self.assertEqual(self.client.get(foreign).status_code, 404)
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.login("editor")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(self.index, self.form_data).status_code, 404)

    def test_new_questions_use_active_evidence_even_with_an_old_form(self):
        self.assertNotContains(self.client.get(self.index), 'name="include_archived"')
        stale_form = self.form_data | {"include_archived": "on"}
        self.assertEqual(self.client.post(self.index, stale_form).status_code, 302)
        first = ChatTurn.objects.get(submission_id=stale_form["submission_id"])
        self.assertFalse(first.include_archived)

        first.status = ChatTurn.Status.COMPLETE
        first.include_archived = True
        first.save(update_fields=("status", "include_archived"))
        detail_url = reverse("core:chat:detail", args=[self.case.pk, first.chat_id])
        detail = self.client.get(detail_url)
        self.assertNotContains(detail, 'name="include_archived"')
        self.assertContains(detail, "Active and archived evidence")
        follow_up = stale_form | {
            "submission_id": uuid4(),
            "question": "Which source records the change?",
        }
        self.assertEqual(self.client.post(detail_url, follow_up).status_code, 302)
        second = ChatTurn.objects.get(submission_id=follow_up["submission_id"])
        self.assertEqual(second.chat_id, first.chat_id)
        self.assertFalse(second.include_archived)

    def test_detail_recent_chats_are_bounded_case_scoped_and_lazy(self):
        in_case = EvidenceChat.objects.bulk_create(
            EvidenceChat(
                case=self.case, created_by=self.users["owner"], title=f"Chat {i}"
            )
            for i in range(11)
        )
        foreign = EvidenceChat.objects.create(
            case=self.other_case,
            created_by=self.users["outsider"],
            title="Unrelated case chat",
        )
        turns = ChatTurn.objects.bulk_create(
            ChatTurn(
                case=chat.case,
                chat=chat,
                asked_by=chat.created_by,
                ordinal=1,
                question=chat.title,
                submission_id=uuid4(),
            )
            for chat in [*in_case, foreign]
        )
        now = timezone.now()
        for i, turn in enumerate(turns):
            turn.created_at = now + timedelta(minutes=i)
        ChatTurn.objects.bulk_update(turns, ["created_at"])
        older_turn = ChatTurn.objects.create(
            case=self.case,
            chat=in_case[-1],
            asked_by=self.users["owner"],
            ordinal=2,
            question="A turn with an older creation timestamp",
            submission_id=uuid4(),
            status="complete",
        )
        foreign_turn = ChatTurn.objects.create(
            case=self.other_case,
            chat=in_case[0],
            asked_by=self.users["outsider"],
            ordinal=2,
            question="Do not account for a cross-case turn",
            submission_id=uuid4(),
            status="complete",
        )
        ChatTurn.objects.filter(pk=older_turn.pk).update(created_at=now)
        ChatTurn.objects.filter(pk=foreign_turn.pk).update(
            created_at=now + timedelta(hours=1)
        )

        self.login("viewer")
        with CaptureQueriesContext(connection) as queries:
            index = self.client.get(self.index)
        self.assertEqual(list(index.context["page_obj"]), list(reversed(in_case)))
        count_queries = [
            query["sql"]
            for query in queries
            if "COUNT(*)" in query["sql"] and "core_evidencechat" in query["sql"]
        ]
        self.assertEqual(len(count_queries), 1)
        self.assertNotIn("core_chatturn", count_queries[0])
        url = reverse("core:chat:detail", args=[self.case.pk, in_case[-1].pk])
        full = self.client.get(url)
        self.assertEqual(
            list(full.context["recent_chats"]), list(reversed(in_case[1:]))
        )
        older_url = reverse("core:chat:detail", args=[self.case.pk, in_case[0].pk])
        older = self.client.get(older_url)
        self.assertContains(older, f'href="{older_url}" aria-current="page"')
        with CaptureQueriesContext(connection) as queries:
            partial = self.client.get(url, headers={"X-Pandect-Live": "1"})
        self.assertEqual(partial.headers["X-Pandect-Live"], "1")
        self.assertFalse(any("last_asked_at" in query["sql"] for query in queries))

    def test_current_destination_and_form_errors_retain_question(self):
        with patch(
            "core.evidence_chat.ai.turn_execution",
            side_effect=AnalysisError("external_processing_not_approved"),
        ):
            response = self.client.post(self.index, self.form_data)
        self.assertContains(response, self.form_data["question"])
        self.assertContains(response, "Review the current setting")
        self.assertFalse(ChatTurn.objects.exists())
        altered = self.form_data | {"route": "unseen"}
        response = self.client.post(self.index, altered)
        self.assertContains(response, "processing destination changed")
        self.assertEqual(
            response.context["form"]["route"].value(), self.form_data["route"]
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.users["owner"])
        self.assertEqual(csrf_client.post(self.index, self.form_data).status_code, 403)

    def test_changed_embedding_destination_requires_a_newly_reviewed_form(self):
        scope = {
            "profile": "synthetic",
            "revision": "base-grant",
            "chat_revision": "chat-grant",
            "destination": {"gateway": "case-search", "model": "test-embeddings"},
        }
        with patch(
            "core.evidence_chat.retrieval_route.hosted_scope", return_value=scope
        ):
            stale = self.client.post(self.index, self.form_data)
            self.assertContains(stale, "processing destination changed")
            self.assertContains(stale, "case-search")
            self.assertContains(stale, "test-embeddings")
            self.assertFalse(ChatTurn.objects.exists())
            self.form_data["route"] = stale.context["route_token"]
            turn = self._ask()
            self.assertEqual(turn.plan["hosted_search"], scope)

    @override_settings(PANDECT_DIRECT_AI={"openai_api_key": "synthetic-direct-key"})
    def test_decisions_transfer_is_visible_and_bound_to_the_submitted_form(self):
        owner = self.users["owner"]
        initial = self.client.get(self.index)
        self.assertNotContains(initial, "OpenAI Decisions")
        review = decisions_approval.approval_context(self.case, owner)
        decisions_approval.change_approval(
            owner, self.case.pk, "approve", review["approve"]
        )
        stale = self.client.post(self.index, self.form_data)
        self.assertContains(stale, "processing destination changed")
        self.assertContains(stale, "OpenAI Decisions")
        self.assertContains(stale, "up to eight short source previews")
        self.assertFalse(ChatTurn.objects.exists())
        self.form_data["route"] = stale.context["route_token"]
        turn = self._ask()
        self.assertEqual(
            turn.plan["decisions_scope"], decisions_approval.scope(self.case)
        )
        review = decisions_approval.approval_context(self.case, owner)
        decisions_approval.change_approval(
            owner, self.case.pk, "revoke", review["revoke"]
        )
        stale = self.client.post(
            self.index, self.form_data | {"submission_id": uuid4()}
        )
        self.assertContains(stale, "processing destination changed")
        self.assertNotContains(stale, "OpenAI Decisions")
        self.assertEqual(ChatTurn.objects.count(), 1)

    def test_pending_chat_status_is_case_scoped_and_contains_only_progress(self):
        turn = self._ask()

        def snapshot(case):
            return self.client.get(
                reverse("cases:activity", args=(case.pk,)),
                {"chat": str(turn.chat_id)},
            ).json()

        self.login("owner")
        self.assertEqual(
            snapshot(self.case)["chat_progress"],
            [str(turn.pk), "queued", "", 0, 0, 0],
        )
        self.login("outsider")
        self.assertIsNone(snapshot(self.other_case)["chat_progress"])

    def test_live_activity_only_links_current_evidence_from_the_chat_case(self):
        source = self.text()
        self.form_data["question"] = "Where is the original café recorded?"
        turn = self._ask()
        reply = reverse("core:chat:reply", args=[self.case.pk, turn.chat_id, turn.pk])
        status = reverse("cases:activity", args=[self.case.pk])
        chat = {"chat": str(turn.chat_id)}
        self.login("viewer")
        self.assertIsNone(self.client.get(status, chat).json()["chat_activity"])
        responses = {}

        def infer(*_):
            responses["fragment"] = self.client.get(
                reply, headers={"X-Pandect-Live": "1"}
            )
            responses["snapshot"] = self.client.get(status, chat)
            self.login("outsider")
            responses["foreign_reply"] = self.client.get(reply)
            responses["foreign_status"] = self.client.get(status, chat)
            self.login("viewer")
            set_evidence_archive(
                self.users["owner"], self.case.pk, "text", source.pk, archived=True
            )
            responses["archived"] = self.client.get(reply)
            raise AnalysisError("source_changed")

        with patch.object(ai, "infer", side_effect=infer) as mock_infer:
            jobs.process_turn(str(turn.pk), str(turn.delivery_token))
        mock_infer.assert_called_once()
        turn.refresh_from_db()
        self.assertEqual(turn.error_code, "source_changed")
        self.assertNotIn("sources", turn.activity)
        self.assertContains(responses["fragment"], "Preparing an answer")
        self.assertContains(responses["fragment"], source.title)
        self.assertContains(responses["fragment"], self.url("detail", source))
        self.assertIsNotNone(responses["snapshot"].json()["chat_activity"])
        self.assertNotContains(responses["snapshot"], source.title)
        self.assertEqual(responses["foreign_reply"].status_code, 404)
        self.assertEqual(responses["foreign_status"].status_code, 404)
        self.assertNotContains(responses["archived"], source.title)
        stopped = self.client.get(reply)
        self.assertContains(stopped, "Review activity")
        self.assertNotContains(stopped, source.title)

    def test_marks_and_notes_validate_the_whole_reply(self):
        source = self.text()
        locator = next(
            row
            for row in corpus.discover(self.case, False)
            if row["evidence_id"] == str(source.pk)
        )
        turn = self._ask()
        ChatSource.objects.create(
            case=self.case,
            turn=turn,
            key=locator["key"],
            record_key=locator["record_key"],
            locator=locator,
            evidence_id=source.pk,
            evidence_kind="text",
        )
        turn.status = "complete"
        turn.include_archived = True
        turn.answer = {
            "claims": [
                {
                    "text": "A witness supplied the original.",
                    "citations": [
                        {
                            "key": locator["key"],
                            "quote": "Original",
                            "start": 0,
                            "end": 8,
                        }
                    ],
                }
            ]
        }
        turn.save(update_fields=("status", "include_archived", "answer"))
        reply = reverse("core:chat:reply", args=[self.case.pk, turn.chat_id, turn.pk])
        mark = reverse("core:important:mark_reply", args=[self.case.pk, turn.pk])
        important = reverse("core:important:list", args=[self.case.pk])
        self.assertContains(self.client.get(reply), "Reference 1")
        self.assertRedirects(self.client.post(mark, {"action": "mark"}), reply)
        saved = ImportantItem.objects.get(chat_turn=turn)
        notes = reverse("core:important:notes", args=[self.case.pk, saved.pk])
        self.assertEqual(
            self.client.post(
                notes, {"body": "Private working annotation."}
            ).status_code,
            302,
        )
        self.login("viewer")
        self.assertContains(self.client.get(important), "Evidence chat reply")
        self.assertContains(self.client.get(notes), "Private working annotation.")
        self.assertEqual(self.client.post(mark, {"action": "unmark"}).status_code, 403)
        self.login("owner")
        self.client.post(self.url("archive", source))
        self.assertContains(self.client.get(reply), "Reply unavailable")
        self.assertNotContains(self.client.get(reply), "A witness supplied")
        self.assertEqual(self.client.get(notes).status_code, 404)
        self.client.post(self.url("restore", source))
        self.assertContains(self.client.get(reply), "Reference 1")
        self.assertContains(self.client.get(notes), "Private working annotation.")
        source.__class__.objects.filter(pk=source.pk).update(
            body="Unavailable source version."
        )
        self.assertContains(self.client.get(reply), "Reply unavailable")
        self.assertNotContains(self.client.get(reply), "A witness supplied")
        self.assertNotContains(self.client.get(important), turn.question)
        self.assertEqual(self.client.get(notes).status_code, 404)

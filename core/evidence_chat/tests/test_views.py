"""Case and source access for the shared chat and Important readers."""

from unittest.mock import patch
from uuid import uuid4

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.errors import AnalysisError
from core.evidence_chat import corpus
from core.models import ChatSource, ChatTurn, ImportantItem
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
        turn.save(update_fields=("status", "answer"))
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
        source.__class__.objects.filter(pk=source.pk).update(
            body="Unavailable source version."
        )
        self.assertContains(self.client.get(reply), "Reply unavailable")
        self.assertNotContains(self.client.get(reply), "A witness supplied")
        self.assertNotContains(self.client.get(important), turn.question)
        self.assertEqual(self.client.get(notes).status_code, 404)

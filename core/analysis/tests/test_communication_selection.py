"""Message-only analyses exclude calls without invalidating retained work."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from core.analysis.forms import analysis_form
from core.analysis.operations import OPERATIONS
from core.analysis.services import process_run, start_run
from core.evidence.types import COMMUNICATION_KINDS
from core.models import AnalysisRun
from core.testing import EvidenceViewTestMixin
from phone.models import (
    CallEvidenceAttachment,
    FaceTimeEvidence,
    PhoneCallEvidence,
    TextMessageEvidence,
)


class MessageAnalysisSelectionTests(EvidenceViewTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        values = {
            "case": self.case,
            "uploaded_by": self.users["owner"],
            "sender": self.person,
            "recipient": self.person,
            "occurred_at": timezone.now(),
        }
        self.message = TextMessageEvidence.objects.create(
            **values, title="Retained message", body="Meet at the harbour office."
        )
        self.calls = {
            "phone-call": PhoneCallEvidence.objects.create(
                **values, title="Reported phone call"
            ),
            "facetime": FaceTimeEvidence.objects.create(
                **values, title="Reported FaceTime call"
            ),
        }
        self.transcripts = {
            kind: CallEvidenceAttachment.objects.create(
                case=self.case,
                uploaded_by=self.users["owner"],
                transcript="We will meet at the harbour office.",
                **{"phone_call" if kind == "phone-call" else "facetime": call},
            )
            for kind, call in self.calls.items()
        }

    def test_message_picker_and_form_exclude_calls_and_their_transcripts(self):
        message_choice = f"text-message:{self.message.pk}"
        for operation in ("sessions", "comm_patterns", "session_summaries"):
            with self.subTest(operation=operation):
                form = analysis_form(case=self.case, operation=operation)
                self.assertEqual(
                    [key for key, _ in form.fields["sources"].choices],
                    [message_choice],
                )
                for kind, call in self.calls.items():
                    for choice in (
                        f"{kind}:{call.pk}",
                        f"{kind}:{call.pk}:{self.transcripts[kind].pk}",
                    ):
                        forged = analysis_form(
                            {
                                "operation": operation,
                                "submission_id": uuid4(),
                                "sources": [message_choice, choice],
                            },
                            case=self.case,
                            operation=operation,
                        )
                        self.assertFalse(forged.is_valid())
                        self.assertIn("sources", forged.errors)

    def test_service_rejects_calls_before_queueing_even_with_a_valid_message(self):
        message = {"kind": "text-message", "id": str(self.message.pk)}
        for operation in ("sessions", "comm_patterns", "session_summaries"):
            for kind, call in self.calls.items():
                with self.subTest(operation=operation, kind=kind):
                    with self.assertRaisesMessage(
                        ValidationError,
                        "This evidence type is not supported by the selected analysis.",
                    ):
                        start_run(
                            self.users["owner"],
                            self.case.pk,
                            operation,
                            [message, {"kind": kind, "id": str(call.pk)}],
                        )
        self.assertFalse(AnalysisRun.objects.exists())
        self.assertFalse(DBTaskResult.objects.exists())
        for operation in ("sessions", "comm_patterns", "session_summaries"):
            run, created = start_run(
                self.users["owner"], self.case.pk, operation, [message]
            )
            self.assertTrue(created)
            self.assertEqual(run.inputs[0]["evidence_id"], str(self.message.pk))

    def test_calls_keep_their_supported_analysis_options(self):
        self.login("editor")
        for kind, call in self.calls.items():
            with self.subTest(kind=kind):
                response = self.client.get(self.url("detail", call, kind))
                self.assertNotContains(response, "?operation=session_summaries")
                self.assertContains(response, "?operation=text_topics")
                self.assertContains(response, "?operation=content_events")
                for operation in ("conversation_link", "text_topics", "content_events"):
                    form = analysis_form(case=self.case, operation=operation)
                    choices = {key for key, _ in form.fields["sources"].choices}
                    self.assertIn(f"{kind}:{call.pk}", choices)
                    self.assertIn(
                        f"{kind}:{call.pk}:{self.transcripts[kind].pk}", choices
                    )

    def test_earlier_mixed_selection_can_finish_and_remain_readable(self):
        operation = OPERATIONS["sessions"]
        selections = [
            {"kind": "text-message", "id": str(self.message.pk)},
            {"kind": "phone-call", "id": str(self.calls["phone-call"].pk)},
        ]
        # Reproduce a queued selection accepted before the picker was narrowed.
        with patch.dict(
            OPERATIONS,
            {"sessions": replace(operation, kinds=COMMUNICATION_KINDS)},
        ):
            run, _ = start_run(
                self.users["owner"], self.case.pk, "sessions", selections
            )
        original_inputs = run.inputs
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        self.assertEqual(run.inputs, original_inputs)
        self.assertEqual(run.result.data["metrics"]["selected_messages"], 1)
        self.login("viewer")
        for route in ("detail", "download"):
            with self.subTest(route=route):
                response = self.client.get(
                    reverse(f"core:analysis:{route}", args=[self.case.pk, run.pk])
                )
                self.assertEqual(response.status_code, 200)
        with self.assertRaises(ValidationError):
            start_run(
                self.users["owner"],
                self.case.pk,
                "sessions",
                selections,
                previous_run=run,
            )

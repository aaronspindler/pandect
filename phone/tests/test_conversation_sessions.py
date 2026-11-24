"""Saved session membership in the retained conversation transcript."""

from datetime import UTC, datetime, timedelta

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.analysis.catalogue import project_result
from core.analysis.operations import get_operation
from core.analysis.services import resolve_inputs, snapshot_sources
from core.models import AnalysisFindingSource, AnalysisResult, AnalysisRun
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.conversation_sessions import attach_session_groups
from phone.models import CommunicationContext, TextMessageEvidence


class ConversationSessionTests(EvidenceViewTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.correspondent = Person.objects.create(case=cls.case, first_name="Blair")
        pair = sorted([cls.person.pk, cls.correspondent.pk])
        cls.reader = reverse("phone:conversation_detail", args=(cls.case.pk, *pair))

    def message(self, minute):
        sender, recipient = self.person, self.correspondent
        if minute % 2:
            sender, recipient = recipient, sender
        return TextMessageEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            sender=sender,
            recipient=recipient,
            title=f"Message {minute}",
            body=f"Synthetic original {minute}",
            occurred_at=datetime(2026, 7, 29, 15, tzinfo=UTC)
            + timedelta(minutes=minute),
        )

    def publish(self, messages, *, gap=60, empty=False):
        operation = get_operation("sessions")
        run = AnalysisRun.objects.create(
            case=self.case,
            requested_by=self.case.owner,
            operation="sessions",
            method_version=operation.version,
            parameters={"gap_minutes": gap},
            inputs=snapshot_sources(
                self.case, [{"kind": "text-message", "id": m.pk} for m in messages]
            ),
            status="empty" if empty else "complete",
        )
        data = (
            {"items": []}
            if empty
            else operation.function(resolve_inputs(run, self.case), run.parameters)
        )
        result = AnalysisResult.objects.create(case=self.case, run=run, data=data)
        project_result(result)
        return result

    def test_delayed_reply_checks_id_ambiguity_outside_the_selected_messages(self):
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Synthetic export",
            file=ContentFile(b"synthetic", name="synthetic.csv"),
        )
        parent, reply, duplicate = [self.message(minute) for minute in (0, 121, 123)]

        def context(message, row, native, pointer=""):
            CommunicationContext.objects.create(
                case=self.case,
                message=message,
                source_document=export,
                source_sha256=export.sha256,
                source_row=row,
                source_start=row - 1,
                source_end=row,
                parser_version="synthetic",
                timezone="UTC",
                raw_sender="Synthetic account",
                native_message_id=native,
                reply_to_id=pointer,
            )

        context(parent, 1, "source-1")
        context(reply, 2, "source-2", "source-1")
        self.assertEqual(len(self.publish([parent, reply]).data["items"]), 1)
        context(duplicate, 3, "source-1")
        self.assertEqual(len(self.publish([parent, reply]).data["items"]), 2)

    def test_full_membership_and_real_endpoints_across_pages(self):
        messages = [self.message(i) for i in range(52)]
        first, uncited, middle, last = messages[-4:]
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.case.owner,
            title="Synthetic export",
            file=ContentFile(b"synthetic", name="synthetic.csv"),
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=middle,
            source_document=export,
            source_sha256=export.sha256,
            source_row=1,
            source_start=0,
            source_end=1,
            parser_version="synthetic",
            timezone="UTC",
            raw_sender="Synthetic account",
        )
        self.publish([last, first, middle])
        self.login("viewer")
        rows = list(self.client.get(self.reader).context["page_obj"])
        start, skipped = rows[-2:]
        self.assertEqual((start.pk, skipped.pk), (first.pk, uncited.pk))
        self.assertEqual(start.session_marker, "Session starts")
        self.assertEqual(skipped.session_groups, [])
        second_page = self.client.get(self.reader, {"page": 2})
        continuation, end = second_page.context["page_obj"]
        self.assertEqual(continuation.session_marker, "")
        self.assertEqual(end.session_marker, "Session ends")
        self.assertEqual(start.session_tint, continuation.session_tint)
        self.assertEqual(start.session_tint, end.session_tint)
        self.assertEqual(continuation.session_groups[0]["count"], 3)
        self.assertIn(
            "First selected message", continuation.session_groups[0]["reason"]
        )

    def test_independent_equal_memberships_and_competing_groupings(self):
        first, shared, last = messages = [self.message(i) for i in range(3)]
        self.publish([first, shared])
        self.publish([first, shared], gap=30)
        latest = self.publish([first, shared], gap=30)
        overlapping = self.publish([shared, last])
        attach_session_groups(self.case, messages)
        self.assertEqual(len(first.session_groups), 1)
        self.assertIn(str(latest.run_id), first.session_groups[0]["url"])
        self.assertIsNone(shared.session_tint)
        self.assertEqual(shared.session_marker, "Multiple groupings")
        self.assertEqual(len(shared.session_groups), 2)
        self.assertIn(str(overlapping.run_id), shared.session_groups[0]["url"])
        self.assertIn(str(latest.run_id), shared.session_groups[1]["url"])
        self.assertEqual(last.session_marker, "Session ends")
        # The newest equivalent result can remove a previously reported grouping.
        self.publish([shared, last], empty=True)
        attach_session_groups(self.case, messages)
        self.assertEqual(shared.session_tint, first.session_tint)
        self.assertEqual(shared.session_marker, "Session ends")
        self.assertEqual(last.session_groups, [])

    def test_incomplete_cross_case_citations_and_stale_inputs_withhold_group(self):
        first, second, third = messages = [self.message(i) for i in range(3)]
        result = self.publish([first, second])
        AnalysisFindingSource.objects.filter(
            finding__result=result, evidence_id=second.pk
        ).update(case=self.other_case)
        self.publish([first, third])
        TextMessageEvidence.objects.filter(pk=third.pk).update(sha256="0" * 64)
        attach_session_groups(self.case, messages)
        self.assertTrue(all(m.session_groups == [] for m in messages))

    def test_sessions_beyond_the_findings_page_are_resolved_in_batches(self):
        messages = [self.message(i * 5) for i in range(30)]
        self.publish(messages, gap=1)
        with CaptureQueriesContext(connection) as one:
            attach_session_groups(self.case, messages[:1])
        with CaptureQueriesContext(connection) as many:
            attach_session_groups(self.case, messages)
        self.assertEqual(len(many), len(one))
        self.assertTrue(
            all(m.session_marker == "Single-message session" for m in messages)
        )
        self.assertNotEqual(messages[0].session_tint, messages[1].session_tint)
        self.assertEqual(messages[-1].session_groups[0]["label"], "Inferred session 30")
        self.assertNotIn(
            ', "phone_textmessageevidence"."body",', str(many.captured_queries)
        )

    def test_distinct_group_preview_overflow_links_to_the_exact_session_catalogue(self):
        shared, *others = [self.message(i) for i in range(27)]
        oldest = self.publish([shared, others[0]])
        for other in others[1:25]:
            self.publish([shared, other])
        self.publish([shared, others[1]], gap=45)
        attach_session_groups(self.case, [shared])
        self.assertEqual(len(shared.session_groups), 25)
        self.assertEqual(shared.session_more_url, "")

        self.publish([shared, others[-1]])
        attach_session_groups(self.case, [shared, others[-1]])
        self.assertEqual(len(shared.session_groups), 25)
        self.assertEqual(shared.session_marker, "Multiple groupings")
        self.assertEqual(others[-1].session_more_url, "")
        self.assertFalse(
            any(str(oldest.run_id) in group["url"] for group in shared.session_groups)
        )
        self.login("viewer")
        catalogue = self.client.get(shared.session_more_url)
        self.assertEqual(catalogue.context["filters"]["method"], "sessions")
        self.assertEqual(catalogue.context["filters"]["kind"], "text-message")
        self.assertEqual(catalogue.context["filters"]["evidence"], shared.pk)
        self.assertEqual(catalogue.context["page_obj"].paginator.count, 27)
        second_page = self.client.get(shared.session_more_url + "&page=2")
        self.assertContains(second_page, str(oldest.run_id))

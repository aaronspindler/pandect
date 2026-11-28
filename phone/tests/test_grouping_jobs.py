"""An instance's exact membership outlives processing and display chunk boundaries."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.automatic import start_added_evidence_analysis
from core.analysis.bulk import prepare_case_analysis_batch, resume_case_analysis
from core.analysis.reprocessing import start_reprocessing
from core.evidence.deletion import plan_deletion
from core.tests.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from people.models import Person
from phone.grouping_jobs import _legacy_scope, grouping_scope, start_grouping
from phone.models import (
    CommunicationContext,
    ConversationGrouping,
    ConversationPairRevision,
    ConversationSessionMember,
    TextMessageEvidence,
)


class GroupingJobTests(EvidenceViewTestMixin, TestCase):
    def messages(self, amount, *, body="Message"):
        first = datetime(2026, 4, 18, 9, tzinfo=UTC)
        originals = []
        for number in range(amount):
            message = TextMessageEvidence(
                case=self.case,
                sender=self.person,
                recipient=self.person,
                uploaded_by=self.users["editor"],
                title=f"Message {number}",
                body=body,
                occurred_at=first + timedelta(minutes=number),
            )
            message.sha256 = sha256(message.original_bytes()).hexdigest()
            originals.append(message)
        TextMessageEvidence.objects.bulk_create(originals, batch_size=100)
        return originals

    def request_grouping(self, submission=None):
        return start_grouping(
            self.users["editor"],
            self.case,
            self.person,
            self.person,
            gap_minutes=60,
            submission_id=submission or uuid4(),
            reviewed_scope=grouping_scope(self.case, self.person, self.person)[1],
        )

    def test_large_session_is_published_once_with_all_exact_members(self):
        messages = self.messages(501, body="Text 🌊\n" * 300)
        submission = uuid4()
        batch, created = self.request_grouping(submission)
        self.assertTrue(created)
        revision = batch.conversation_groupings.get()
        with patch("phone.grouping_jobs.PAGE_SIZE", 100):
            for _ in range(5):
                self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
                revision.refresh_from_db()
                self.assertIsNone(revision.published_at)
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(self.request_grouping(submission), (batch, False))
        session = revision.sessions.get()
        self.assertEqual(session.message_count, 501)
        self.assertEqual(
            list(
                session.members.order_by("ordinal").values_list("message_id", flat=True)
            ),
            [m.pk for m in messages],
        )

    def test_date_only_original_does_not_supply_a_session_clock(self):
        dated, timed = self.messages(2)
        export = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.users["editor"],
            title="Retained export",
            file=ContentFile(b"Synthetic originals", name="messages.csv"),
        )
        CommunicationContext.objects.create(
            case=self.case,
            message=dated,
            source_document=export,
            source_sha256=export.sha256,
            source_row=1,
            source_start=0,
            source_end=1,
            parser_version="synthetic-v1",
            timezone="America/Toronto",
            raw_sender="Reported sender",
            raw_timestamp="2026-04-18",
            native_message_id="day-only",
        )
        batch, _ = self.request_grouping()
        with patch("phone.grouping_jobs.PAGE_SIZE", 1):
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")

        known, unknown = batch.conversation_groupings.get().sessions.order_by(
            "start_ordinal"
        )
        self.assertEqual((known.first_at, known.last_at), (timed.occurred_at,) * 2)
        self.assertEqual(unknown.members.get().message_id, dated.pk)
        self.assertIsNone(unknown.first_at)
        self.assertIsNone(unknown.last_at)
        self.assertIsNone(unknown.members.get().elapsed_seconds)
        dated.refresh_from_db()
        self.assertIsNotNone(dated.occurred_at)

    def test_new_grouping_uses_pair_revisions_across_bulk_writes(self):
        first = self.messages(1)[0]
        scope = grouping_scope(self.case, self.person, self.person)
        TextMessageEvidence.objects.filter(pk=first.pk).update(title="Edited title")
        self.assertEqual(grouping_scope(self.case, self.person, self.person), scope)
        # A bulk content edit changes the selection marker even when its count
        # and the stored checksum have been left untouched.
        TextMessageEvidence.objects.filter(pk=first.pk).update(body="Changed")
        self.assertNotEqual(grouping_scope(self.case, self.person, self.person), scope)
        TextMessageEvidence.objects.filter(pk=first.pk).update(body=first.body)
        with patch(
            "phone.grouping_jobs._legacy_scope",
            side_effect=AssertionError("Revision-backed grouping must not rescan"),
        ):
            batch, _ = self.request_grouping()
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")

    def test_grouping_from_before_revision_tracking_keeps_its_exact_scope(self):
        self.messages(2)
        batch, _ = self.request_grouping()
        _, previous_digest = _legacy_scope(self.case, self.person, self.person)
        ConversationGrouping.objects.filter(batch=batch).update(
            scope_digest=previous_digest
        )
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        grouping = batch.conversation_groupings.get()
        self.assertEqual(grouping.processed_digest, previous_digest)
        self.assertIsNotNone(grouping.published_at)

    def test_case_deletion_does_not_recreate_pair_revisions(self):
        self.messages(1)
        case_id = self.case.pk
        self.assertTrue(
            ConversationPairRevision.objects.filter(case_id=case_id).exists()
        )
        self.case.delete()
        self.assertFalse(
            ConversationPairRevision.objects.filter(case_id=case_id).exists()
        )

    def test_automatic_requests_share_work_when_the_first_requester_loses_access(self):
        first, second, third = self.messages(3)
        jobs = [
            start_added_evidence_analysis(
                actor, self.case.pk, "text-message", message.pk
            )[0]
            for actor, message in (
                (self.users["editor"], first),
                (self.users["owner"], second),
                (self.users["owner"], third),
            )
        ]
        with patch("phone.grouping_jobs.PAGE_SIZE", 1):
            self.assertEqual(prepare_case_analysis_batch(jobs[0].pk), "queued")
            self.assertEqual(prepare_case_analysis_batch(jobs[1].pk), "queued")
            revision = ConversationGrouping.objects.get()
            self.assertEqual(revision.processed_count, 2)
            CaseMembership.objects.filter(
                case=self.case, user=self.users["editor"]
            ).delete()
            self.assertEqual(prepare_case_analysis_batch(jobs[0].pk), "access_removed")
            self.assertEqual(prepare_case_analysis_batch(jobs[1].pk), "queued")
            self.assertEqual(prepare_case_analysis_batch(jobs[1].pk), "complete")
            self.assertEqual(prepare_case_analysis_batch(jobs[2].pk), "complete")
        revision.refresh_from_db()
        self.assertEqual(revision.published_by, self.users["owner"])
        self.assertEqual(revision.requests.count(), 3)
        self.assertEqual(ConversationSessionMember.objects.count(), 3)
        self.assertEqual(ConversationGrouping.objects.count(), 1)

    def test_stale_pair_does_not_block_other_pairs_in_the_same_batch(self):
        first, _ = self.messages(2)
        other = Person.objects.create(case=self.case, first_name="Other")
        TextMessageEvidence.objects.create(
            case=self.case,
            sender=self.person,
            recipient=other,
            uploaded_by=self.users["owner"],
            title="Another pair",
            body="Hello",
            occurred_at=first.occurred_at,
        )
        batch, _ = start_reprocessing(
            self.users["owner"],
            self.case.pk,
            submission_id=uuid4(),
            kinds=["text-message"],
        )
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
        self.messages(1)
        for _ in range(5):
            if prepare_case_analysis_batch(batch.pk) == "complete":
                break
        batch.refresh_from_db()
        self.assertEqual(batch.status, "complete")
        self.assertEqual(batch.unavailable_sources, 1)
        stale = batch.grouping_requests.get(error_code="grouping_scope_changed")
        self.assertIsNone(stale.grouping.published_at)
        published = batch.grouping_requests.get(error_code="").grouping
        self.assertIsNotNone(published.published_at)
        self.assertEqual(published.sessions.get().message_count, 1)
        self.login("editor")
        page = self.client.get(
            reverse("core:analysis:processing", args=[self.case.pk]),
            {"batch": batch.pk},
        )
        self.assertContains(page, "Review conversation grouping")
        self.assertContains(
            page,
            reverse(
                "phone:group_conversation",
                args=[self.case.pk, self.person.pk, self.person.pk],
            ),
        )

    def test_changed_selection_keeps_the_previous_publication(self):
        self.messages(4)
        previous, _ = self.request_grouping()
        self.assertEqual(prepare_case_analysis_batch(previous.pk), "complete")
        later, _ = self.request_grouping()
        with patch("phone.grouping_jobs.PAGE_SIZE", 3):
            self.assertEqual(prepare_case_analysis_batch(later.pk), "queued")
            self.messages(1)
            self.assertEqual(
                prepare_case_analysis_batch(later.pk), "grouping_scope_changed"
            )
        self.assertIsNotNone(previous.conversation_groupings.get().published_at)
        self.assertIsNone(later.conversation_groupings.get().published_at)

    def test_retry_resumes_the_last_committed_page_and_checks_access(self):
        self.messages(4)
        batch, _ = self.request_grouping()
        with patch("phone.grouping_jobs.PAGE_SIZE", 3):
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
            with patch.object(
                ConversationSessionMember.objects,
                "bulk_create",
                side_effect=RuntimeError,
            ):
                self.assertEqual(
                    prepare_case_analysis_batch(batch.pk), "preparation_failed"
                )
            self.assertEqual(batch.conversation_groupings.get().processed_count, 3)
            self.assertTrue(
                resume_case_analysis(self.users["editor"], self.case.pk, batch.pk)[1]
            )
            self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(ConversationSessionMember.objects.count(), 4)
        revoked, _ = self.request_grouping()
        CaseMembership.objects.filter(
            case=self.case, user=self.users["editor"]
        ).delete()
        self.assertEqual(prepare_case_analysis_batch(revoked.pk), "access_removed")
        self.assertIsNone(revoked.conversation_groupings.get().published_at)

    def test_changed_long_body_fails_integrity_and_deletion_includes_all_members(self):
        first, second = self.messages(2, body='Unicode 💡 "quote"\n\\' * 6000)
        batch, _ = self.request_grouping()
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        group = batch.conversation_groupings.get().sessions.get()
        inventory = plan_deletion(first, "text-message")
        self.assertFalse(inventory.blocker)
        confirmed = {entry["id"] for entry in inventory.entries}
        self.assertIn(str(group.pk), confirmed)
        self.assertLessEqual(
            {str(pk) for pk in group.members.values_list("pk", flat=True)}, confirmed
        )
        self.assertNotIn(str(second.pk), confirmed)
        later, _ = self.request_grouping()
        TextMessageEvidence.objects.filter(pk=second.pk).update(
            body=second.body + "changed"
        )
        self.assertEqual(prepare_case_analysis_batch(later.pk), "source_changed")
        self.assertIsNone(later.conversation_groupings.get().published_at)

    def test_stale_manual_review_cannot_queue_a_different_selection(self):
        self.messages(1)
        _, digest = grouping_scope(self.case, self.person, self.person)
        self.messages(1)
        with self.assertRaises(ValidationError):
            start_grouping(
                self.users["editor"],
                self.case,
                self.person,
                self.person,
                gap_minutes=60,
                submission_id=uuid4(),
                reviewed_scope=digest,
            )

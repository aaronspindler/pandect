"""Worker, approval and delivery of one case-scoped local PDF extraction."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4, uuid5

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import close_old_connections
from django.http import Http404
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from cases.models import CaseMembership
from core.analysis.automatic import (
    start_added_evidence_analysis,
    start_evidence_analysis,
)
from core.analysis.bulk import prepare_case_analysis_batch
from core.analysis.errors import AnalysisError
from core.analysis.execution import configured_external_executions
from core.analysis.services import cancel_run, process_run, start_run
from core.analysis.testing import approve_case_execution
from core.analysis.tests.test_openai_gateway import OPENAI
from core.evidence.deletion import plan_deletion
from core.evidence.retention import set_evidence_archive
from core.evidence_chat import ai, jobs, proposals, services
from core.evidence_chat.tests.test_decisions_worker import answer_from_quotes
from core.evidence_chat.tests.test_engine import decision
from core.models import (
    AnalysisResult,
    AnalysisRun,
    CaseAnalysisBatch,
    ChatAnalysisProposal,
    ChatTurn,
)
from core.testing import EvidenceViewTestMixin
from document.models import DocumentEvidence
from document.tests.test_analysis import pdf_bytes


class ProposalFixtures(EvidenceViewTestMixin):
    def setUp(self):
        super().setUp()
        self.owner, self.editor = self.users["owner"], self.users["editor"]
        self.case.ai_backend = "gateway"
        self.case.save(update_fields=["ai_backend"])
        approve_case_execution(
            self.case, self.owner, configured_external_executions(case=self.case)
        )
        self.document = DocumentEvidence.objects.create(
            case=self.case,
            uploaded_by=self.owner,
            title="Delivery receipt",
            file=ContentFile(pdf_bytes("Pickup at 15:15."), name="receipt.pdf"),
        )
        self.question = "What pickup time does the delivery receipt show?"
        self.calls = []

        def model(turn, authorize, prompt, schema):
            authorize()
            records = json.loads(prompt.split("\n", 1)[1])["records"]
            self.calls.append(records)
            if any("Pickup at 15:15." in r["text"] for r in records):
                return answer_from_quotes(records, ["Pickup at 15:15."])
            doc = next(r for r in records if r["source"].startswith("Document"))
            return decision(
                "propose_extraction", missing="Read the pickup time from the receipt."
            ) | {"source": doc["id"]}

        self.model = self.enterContext(patch.object(ai, "infer", side_effect=model))

    def ask(self):
        parent = services.submit_turn(
            self.editor, self.case.pk, self.question, submission_id=uuid4()
        )
        jobs.process_turn(parent.pk, parent.delivery_token)
        parent.refresh_from_db()
        self.assertEqual(parent.status, "complete", parent.error_code)
        return parent

    def respond(self, proposal, actor=None, action="approve"):
        actor = actor or self.owner
        return proposals.respond(
            actor,
            self.case.pk,
            proposal.parent.chat_id,
            proposal.parent_id,
            action,
            proposals.reviewed_token(proposal, actor),
        )

    def manual(self, submission_id=None):
        return start_run(
            self.editor,
            self.case.pk,
            "native_text",
            proposals._selection(self.document),
            submission_id=submission_id,
        )


@override_settings(PANDECT_AI_GATEWAY=OPENAI)
class ProposalTests(ProposalFixtures, TestCase):
    def test_approved_extraction_waits_for_active_turn_and_resumes_exactly_once(self):
        parent = self.ask()
        original = parent.answer
        check = parent.analysis_proposal
        self.assertFalse(AnalysisRun.objects.exists())
        approved = self.respond(check)
        self.assertEqual(approved.run.requested_by_id, self.owner.pk)
        self.respond(check, self.editor)
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.client.force_login(self.owner)
        url = reverse("core:chat:detail", args=(self.case.pk, parent.chat_id))
        page = self.client.get(url)
        self.assertContains(page, 'data-live-poll="true"')
        self.assertContains(page, 'aria-label="Ask a follow-up"')
        form = page.context["form"]
        key = form["submission_id"].value()
        sent = self.client.post(
            url,
            {
                "question": "What else was reported?",
                "submission_id": key,
                "route": form["route"].value(),
            },
        )
        self.assertEqual(sent.status_code, 302)
        intervening = ChatTurn.objects.get(chat_id=parent.chat_id, submission_id=key)
        self.assertNotContains(self.client.get(url), 'aria-label="Ask a follow-up"')
        self.assertEqual(
            process_run(approved.run_id),
            str(AnalysisRun.objects.get(pk=approved.run_id).result.pk),
        )
        approved.refresh_from_db()
        self.assertIsNone(approved.continuation_id)
        with patch.object(ai, "infer", return_value=decision("answer")):
            jobs.process_turn(intervening.pk, intervening.delivery_token)
        approved.refresh_from_db()
        child = approved.continuation
        self.assertEqual(child.ordinal, 3)
        self.assertEqual(child.asked_by_id, self.editor.pk)
        self.assertEqual(child.question, self.question)
        jobs.process_turn(child.pk, child.delivery_token)
        child.refresh_from_db()
        self.assertEqual(child.status, "complete", child.error_code)
        citation = services.inspect_turn(child)["claims"][0]["citations"][0]
        self.assertEqual(citation["quote"], "Pickup at 15:15.")
        self.assertIn(str(approved.run_id), citation["url"])
        self.assertIn("Page 1", citation["label"])
        approved.run.refresh_from_db()
        proposals.run_finished(approved.run)
        self.assertEqual(ChatTurn.objects.filter(chat=parent.chat).count(), 3)
        parent.refresh_from_db()
        self.assertEqual(parent.answer, original)
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_exhausted_linked_reply_completes_without_another_extraction_or_model_call(
        self,
    ):
        check = self.respond(self.ask().analysis_proposal)
        process_run(check.run_id)
        check.refresh_from_db()
        child = check.continuation
        child.plan["lookup_attempts"] = 4
        child.save(update_fields=["plan"])
        self.calls.clear()
        jobs.process_turn(child.pk, child.delivery_token)
        child.refresh_from_db()
        self.assertEqual(child.status, "complete", child.error_code)
        self.assertEqual(services.inspect_turn(child)["claims"], [])
        self.assertIn("limit", child.answer["summary"].lower())
        self.assertFalse(self.calls)
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_saved_result_answers_without_new_job_or_approval(self):
        run, _ = self.manual()
        process_run(run.pk)
        parent = self.ask()
        self.assertFalse(ChatAnalysisProposal.objects.exists())
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(
            services.inspect_turn(parent)["claims"][0]["citations"][0]["quote"],
            "Pickup at 15:15.",
        )

    def test_decline_and_stale_review_cannot_start_work(self):
        check = self.ask().analysis_proposal
        with self.assertRaises(ValidationError):
            proposals.respond(
                self.owner,
                self.case.pk,
                check.parent.chat_id,
                check.parent_id,
                "approve",
                "",
            )
        with self.assertRaises((Http404, PermissionDenied)):
            self.respond(check, self.users["viewer"])
        with self.assertRaises(Http404):
            self.respond(check, self.users["outsider"])
        url = reverse(
            "core:chat:proposal",
            args=(self.case.pk, check.parent.chat_id, check.parent_id),
        )
        self.client.force_login(self.owner)
        page = self.client.get(url)
        self.assertContains(page, "Approve and run")
        self.assertContains(page, "Selected passages from this local extraction")
        self.assertContains(page, self.document.sha256)
        self.client.post(
            url,
            {
                "action": "decline",
                "confirmation": proposals.reviewed_token(check, self.owner),
            },
        )
        check.refresh_from_db()
        self.assertEqual(check.status, "declined")
        self.respond(check)
        self.assertFalse(AnalysisRun.objects.exists())

    def test_running_automatic_reuse_preserves_followups(self):
        check = self.ask().analysis_proposal
        runs, _ = start_evidence_analysis(
            self.editor,
            self.case.pk,
            "document",
            self.document.pk,
            submission_id=uuid4(),
        )
        approved = self.respond(check)
        self.assertEqual(approved.run_id, runs[0].pk)
        process_run(approved.run_id)
        self.assertTrue(
            AnalysisRun.objects.filter(
                operation="entities", previous_run_id=approved.run_id
            ).exists()
        )

    def test_automatic_after_chat_honors_followups_and_manual_submission_identity(self):
        check = self.respond(self.ask().analysis_proposal)
        key = uuid4()
        manual, created = self.manual(key)
        self.assertFalse(created)
        self.assertEqual(manual.pk, check.run_id)
        runs, created = start_evidence_analysis(
            self.editor,
            self.case.pk,
            "document",
            self.document.pk,
            submission_id=uuid4(),
        )
        self.assertFalse(created)
        self.assertEqual(runs[0].pk, check.run_id)
        process_run(check.run_id)
        again, created = self.manual(key)
        self.assertFalse(created)
        self.assertEqual(again.pk, manual.pk)
        self.assertTrue(
            AnalysisRun.objects.filter(
                operation="entities",
                requested_by=self.editor,
                previous_run_id=manual.pk,
            ).exists()
        )

    def test_reused_submission_id_rejects_a_different_analysis_operation(self):
        check = self.respond(self.ask().analysis_proposal)
        key = uuid4()
        run, created = self.manual(key)
        self.assertFalse(created)
        self.assertEqual(run.pk, check.run_id)
        with self.assertRaisesMessage(ValidationError, "different analysis settings"):
            start_run(
                self.editor,
                self.case.pk,
                "ocr",
                proposals._selection(self.document),
                submission_id=key,
            )
        self.assertEqual(AnalysisRun.objects.count(), 1)
        self.assertEqual(self.manual(key), (run, False))

    def test_on_add_batch_and_chat_share_extraction_and_publish_batch_followups(self):
        check = self.respond(self.ask().analysis_proposal)
        batch, _ = start_added_evidence_analysis(
            self.editor, self.case.pk, "document", self.document.pk
        )
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(AnalysisRun.objects.filter(operation="native_text").count(), 1)
        process_run(check.run_id)
        self.assertTrue(
            batch.runs.filter(
                previous_run_id=check.run_id, operation="entities"
            ).exists()
        )

    def test_legacy_bulk_reuse_publishes_chat_result_and_both_ordinary_followups(self):
        check = self.respond(self.ask().analysis_proposal)
        batch = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.editor, scope={}
        )
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "queued")
        self.assertEqual(prepare_case_analysis_batch(batch.pk), "complete")
        self.assertEqual(AnalysisRun.objects.count(), 1)
        process_run(check.run_id)
        check.refresh_from_db()
        self.assertEqual(check.run.status, "complete", check.run.error_code)
        self.assertIsNotNone(check.continuation_id)
        marker = uuid5(batch.pk, f"document:{self.document.pk}")
        followups = AnalysisRun.objects.filter(previous_run=check.run)
        self.assertSetEqual(
            set(followups.values_list("operation", flat=True)),
            {"entities", "text_topics"},
        )
        for followup in followups:
            self.assertEqual(followup.batch_id, batch.pk)
            self.assertEqual(followup.requested_by_id, self.editor.pk)
            self.assertEqual(followup.parameters["automatic_batch"], str(marker))
            self.assertEqual(followup.submission_id, uuid5(marker, followup.operation))

    def test_standalone_reuse_keeps_its_followups_out_of_original_batch(self):
        batch = CaseAnalysisBatch.objects.create(
            case=self.case, requested_by=self.owner, scope={}
        )
        prepare_case_analysis_batch(batch.pk)
        run = batch.runs.get(operation="native_text")
        marker = uuid4()
        runs, created = start_evidence_analysis(
            self.editor,
            self.case.pk,
            "document",
            self.document.pk,
            submission_id=marker,
        )
        self.assertFalse(created)
        self.assertEqual(runs, [run])
        process_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "complete", run.error_code)
        ordinary = AnalysisRun.objects.filter(
            previous_run=run, parameters__automatic_batch=str(marker)
        )
        self.assertEqual(ordinary.count(), 2)
        self.assertFalse(
            ordinary.exclude(batch=None, requested_by=self.editor).exists()
        )
        self.assertEqual(
            batch.runs.filter(previous_run=run, requested_by=self.owner).count(), 2
        )

    def test_existing_on_add_batch_is_reused_despite_its_scheduling_markers(self):
        batch, _ = start_added_evidence_analysis(
            self.editor, self.case.pk, "document", self.document.pk
        )
        prepare_case_analysis_batch(batch.pk)
        run = batch.runs.get(operation="native_text")
        check = self.respond(self.ask().analysis_proposal)
        self.assertEqual(check.run_id, run.pk)
        process_run(run.pk)
        self.assertTrue(
            batch.runs.filter(previous_run=run, operation="entities").exists()
        )

    def test_saved_empty_result_does_not_prompt_to_repeat_extraction(self):
        run, _ = self.manual()
        run.status = "empty"
        run.save(update_fields=["status"])
        AnalysisResult.objects.create(
            case=self.case, run=run, data={"items": [], "outcome": "empty"}
        )
        parent = self.ask()
        self.assertIn("no readable passages", parent.answer["summary"].lower())
        self.assertFalse(ChatAnalysisProposal.objects.exists())
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_new_result_at_approval_is_reused_and_partial_coverage_is_retained(self):
        check = self.ask().analysis_proposal
        run, _ = self.manual()
        process_run(run.pk)
        run.refresh_from_db()
        run.status = "partial"
        run.save(update_fields=["status"])
        approved = self.respond(check)
        approved.refresh_from_db()
        self.assertEqual(approved.run_id, run.pk)
        child = approved.continuation
        jobs.process_turn(child.pk, child.delivery_token)
        child.refresh_from_db()
        self.assertIn("partial coverage", child.answer["summary"])
        self.assertEqual(
            services.inspect_turn(child)["claims"][0]["citations"][0]["quote"],
            "Pickup at 15:15.",
        )
        self.assertEqual(AnalysisRun.objects.count(), 1)

    def test_changed_destination_or_archived_source_requires_a_fresh_review(self):
        check = self.ask().analysis_proposal
        self.case.ai_backend = "local"
        self.case.save(update_fields=["ai_backend"])
        with self.assertRaises(AnalysisError):
            self.respond(check)
        self.case.ai_backend = "gateway"
        self.case.save(update_fields=["ai_backend"])
        set_evidence_archive(
            self.owner, self.case.pk, "document", self.document.pk, archived=True
        )
        with self.assertRaises(AnalysisError):
            self.respond(check)
        self.assertFalse(AnalysisRun.objects.exists())

    def test_revoked_chat_grant_stops_a_linked_answer_before_transfer(self):
        check = self.respond(self.ask().analysis_proposal)
        process_run(check.run_id)
        check.refresh_from_db()
        child = check.continuation
        self.calls.clear()
        self.case.external_analysis_approval = {}
        self.case.save(update_fields=["external_analysis_approval"])
        jobs.process_turn(child.pk, child.delivery_token)
        child.refresh_from_db()
        self.assertEqual(child.status, "failed")
        self.assertFalse(self.calls)

    def test_pdf_deletion_inventories_the_proposal_and_its_linked_replies(self):
        check = self.respond(self.ask().analysis_proposal)
        self.manual()
        process_run(check.run_id)
        check.refresh_from_db()
        jobs.process_turn(check.continuation_id, check.continuation.delivery_token)
        inventory = plan_deletion(self.document, "document")
        self.assertFalse(inventory.blocker)
        ids = {entry["id"] for entry in inventory.entries if "id" in entry}
        self.assertTrue(
            {str(check.pk), str(check.parent_id), str(check.continuation_id)} <= ids
        )

    def test_cancelled_check_has_no_continuation(self):
        check = self.respond(self.ask().analysis_proposal)
        cancel_run(self.owner, self.case.pk, check.run_id)
        check.refresh_from_db()
        self.assertEqual(check.status, "stopped")
        self.assertIsNone(check.continuation_id)

    def test_revoked_requester_cannot_send_extracted_material(self):
        check = self.respond(self.ask().analysis_proposal)
        process_run(check.run_id)
        check.refresh_from_db()
        child = check.continuation
        self.calls.clear()
        CaseMembership.objects.filter(case=self.case, user=self.editor).delete()
        jobs.process_turn(child.pk, child.delivery_token)
        child.refresh_from_db()
        self.assertEqual(child.error_code, "access_removed")
        self.assertFalse(self.calls)


@override_settings(PANDECT_AI_GATEWAY=OPENAI)
class ConcurrentProposalTests(ProposalFixtures, TransactionTestCase):
    def setUp(self):
        EvidenceViewTestMixin.setUpTestData.__func__(type(self))
        super().setUp()

    def test_two_editors_manual_and_automatic_requests_share_one_run(self):
        check = self.ask().analysis_proposal
        barrier = Barrier(4)

        def approve_or_start(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                if action == "automatic":
                    runs, _ = start_evidence_analysis(
                        self.owner,
                        self.case.pk,
                        "document",
                        self.document.pk,
                        submission_id=uuid4(),
                    )
                    return runs[0].pk
                if action == "manual":
                    return self.manual()[0].pk
                return self.respond(check, self.users[action]).run_id
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(
                pool.map(approve_or_start, ("manual", "owner", "editor", "automatic"))
            )
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(AnalysisRun.objects.count(), 1)

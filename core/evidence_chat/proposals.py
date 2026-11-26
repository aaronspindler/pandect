"""Approve missing PDF text extraction against the evidence at the original cutoff."""

from uuid import uuid4

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.crypto import constant_time_compare

from cases.permissions import get_case_for_verified_user
from core.analysis.errors import AnalysisError
from core.analysis.execution import execution_confirmation
from core.analysis.extraction_reuse import (
    ACTIVE,
    PUBLISHED,
    equivalent_extractions,
    find_extraction,
    identified_pdf,
)
from core.analysis.operations import get_operation
from core.analysis.services import start_run
from core.analysis.source_selection import snapshot_sources
from core.evidence.queries import scoped_evidence
from core.models import ChatAnalysisProposal, ChatTurn
from core.parser_contract import MAX_BYTES
from core.storage import private_file

from . import ai, corpus, engine


def _document(case, pk):
    item = scoped_evidence(case, "document").filter(pk=pk, is_archived=False).first()
    if (
        not item
        or not item.file
        or not 0 < item.byte_size <= min(MAX_BYTES, settings.PANDECT_ANALYSIS_MAX_BYTES)
        or not identified_pdf(item)
        or not corpus.available_file(case, item)
    ):
        raise AnalysisError("source_unavailable")
    return item


def _selection(document):
    return [{"kind": "document", "id": str(document.pk)}]


def check_offer(proposal):
    from .services import inspect_turn

    parent = proposal.parent
    try:
        case = get_case_for_verified_user(parent.asked_by_id, parent.case_id, edit=True)
    except PermissionDenied, Http404:
        raise AnalysisError("access_removed") from None
    if parent.chat.case_id != case.pk or parent.status != "complete":
        raise AnalysisError("source_changed")
    document = _document(case, proposal.document_id)
    if (
        document.sha256 != proposal.sha256
        or get_operation("native_text").version != proposal.method_version
        or inspect_turn(parent) is None
    ):
        raise AnalysisError("source_changed")
    if ai.turn_execution(case) != (parent.execution, parent.approval_revision):
        raise AnalysisError("execution_changed")
    return document


def prepare_agent_proposal(turn, selected, purpose, windows, result):
    loc = selected.locator
    if loc["surface"] != "original" or loc["kind"] != "document":
        raise AnalysisError("model_citation_source")
    doc = _document(turn.case, loc["pk"])
    if doc.created_at > turn.started_at:
        raise AnalysisError("source_changed")
    version = get_operation("native_text").version
    run = find_extraction(
        turn.case_id,
        snapshot_sources(turn.case, _selection(doc)),
        version,
        published=True,
    )
    if run or ChatAnalysisProposal.objects.filter(parent=turn).exists():
        result["summary"] = (
            "The extraction is processing. An updated answer can use its results."
            if run and run.status in ACTIVE
            else "This extraction was already checked. Its coverage is limited."
        )
        return
    turn.plan["proposed_extraction"] = {
        "document": str(doc.pk),
        "sha256": doc.sha256,
        "method_version": version,
        "purpose": purpose,
        "context": [[w.locator["key"], w.start] for w in windows],
    }


def record_proposal(turn):
    # Only reached inside the same transaction as publication of the original reply.
    details = turn.plan.pop("proposed_extraction", None)
    if details:
        ChatAnalysisProposal.objects.create(
            parent=turn,
            document_id=details["document"],
            sha256=details["sha256"],
            purpose=details["purpose"],
            method_version=details["method_version"],
            context=details["context"],
        )


def reviewed_token(proposal, actor):
    return execution_confirmation(
        "chat.approve.extraction",
        [
            str(proposal.pk),
            str(actor.pk),
            str(proposal.parent_id),
            str(proposal.document_id),
            proposal.sha256,
            proposal.method_version,
            proposal.parent.approval_revision,
        ],
        proposal.parent.execution,
    )


@transaction.atomic
def respond(actor, case_id, chat_id, turn_id, action, token):
    case = get_case_for_verified_user(actor.pk, case_id, edit=True, lock=True)
    proposal = get_object_or_404(
        ChatAnalysisProposal.objects.select_related("parent__chat").select_for_update(
            of=("self",)
        ),
        parent_id=turn_id,
        parent__case=case,
        parent__chat_id=chat_id,
        parent__chat__case=case,
        document__case=case,
    )
    if action not in ("approve", "decline") or not constant_time_compare(
        token, reviewed_token(proposal, actor)
    ):
        raise ValidationError("Review the proposed check again.")
    if proposal.status != "offered":
        return proposal
    if action == "decline":
        proposal.status = "declined"
        proposal.save(update_fields=["status"])
        return proposal
    doc = check_offer(proposal)
    try:
        with private_file(doc, case.pk).open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise AnalysisError("unsupported_document_format")
    except OSError:
        raise AnalysisError("source_unavailable") from None
    run, _ = start_run(
        actor,
        case.pk,
        "native_text",
        _selection(doc),
        submission_id=proposal.pk,
        reuse_published_extraction=True,
    )
    proposal.approved_by = actor
    proposal.run = run
    proposal.status = "approved"
    proposal.save(update_fields=["approved_by", "run", "status"])
    if run.status in PUBLISHED:
        resume_idle_chat(case.pk, chat_id)
    return proposal


def run_finished(run):
    """Inside the terminal run transaction, with the case locked before its work."""
    waiting = ChatAnalysisProposal.objects.filter(
        run=run,
        parent__case_id=run.case_id,
        parent__chat__case_id=run.case_id,
        document__case_id=run.case_id,
        status="approved",
        continuation__isnull=True,
    )
    if run.status not in PUBLISHED:
        waiting.update(
            status="stopped", error_code=run.error_code or "processing_cancelled"
        )
        return
    for chat_id in waiting.values_list("parent__chat_id", flat=True).distinct():
        resume_idle_chat(run.case_id, chat_id)


def resume_idle_chat(case_id, chat_id):
    """Link and enqueue atomically under the same case lock as ordinary submissions."""
    from .checkpoints import enqueue_turn

    ready = (
        ChatAnalysisProposal.objects.filter(
            status="approved",
            continuation__isnull=True,
            parent__case_id=case_id,
            parent__chat_id=chat_id,
            parent__chat__case_id=case_id,
            document__case_id=case_id,
            run__case_id=case_id,
            run__status__in=PUBLISHED,
            run__result__case_id=case_id,
        )
        .select_related("parent")
        .order_by("parent__ordinal")
        .first()
    )
    if ready is None:
        return
    turns = ChatTurn.objects.filter(chat_id=chat_id, case_id=case_id)
    if turns.filter(status__in=ACTIVE).exists():
        return
    parent = ready.parent
    # Only the original question gains access to this newly approved result.
    # The cutoff, usage ledger, original inputs and other tool references survive.
    try:
        check_offer(ready)
    except AnalysisError as exc:
        ready.status = "stopped"
        ready.error_code = exc.code
        ready.save(update_fields=["status", "error_code"])
        return
    if parent.plan.get("agent_version") != 1:
        ready.status = "stopped"
        ready.error_code = "chat_agent_cutover"
        ready.save(update_fields=["status", "error_code"])
        return
    parent.status = "queued"
    parent.completed_at = None
    parent.delivery_token = uuid4()
    parent.plan.update(
        stage="approved_extraction",
        approved_proposal=str(ready.pk),
        saved_extraction=str(ready.run_id),
    )
    parent.save(update_fields=("status", "completed_at", "delivery_token", "plan"))
    ready.continuation = parent
    ready.save(update_fields=["continuation"])
    enqueue_turn(parent)


def check_continuation(turn):
    proposal = (
        ChatAnalysisProposal.objects.select_related("run", "document")
        .filter(
            pk=turn.plan["approved_proposal"],
            parent=turn,
            continuation=turn,
            run__case_id=turn.case_id,
            document__case_id=turn.case_id,
            run__status__in=PUBLISHED,
            run__result__case_id=turn.case_id,
            status="approved",
        )
        .first()
    )
    if proposal is None:
        raise AnalysisError("source_changed")
    doc = _document(turn.case, proposal.document_id)
    if (
        doc.sha256 != proposal.sha256
        or doc.created_at > turn.started_at
        or not equivalent_extractions(
            turn.case_id,
            snapshot_sources(turn.case, _selection(doc)),
            proposal.method_version,
        )
        .filter(pk=proposal.run_id)
        .exists()
    ):
        raise AnalysisError("source_changed")
    return proposal


def approved_agent_passages(turn):
    proposal = check_continuation(turn)
    rows = (
        corpus.rows(turn.case, "passage", "", False)
        .filter(
            result__run_id=proposal.run_id,
            kind="document",
            evidence_id=proposal.document_id,
        )
        .order_by("item_index", "pk")
    )
    windows = []
    for row in corpus.clipped(rows, "text", size=1200)[:6]:
        material = corpus.material(
            turn.case, "passage", "document", row, False, size=1200
        )
        if material and material.text:
            windows.append(engine.Window(material.locator, 0, material.text))
    turn.plan["approved_result_at"] = proposal.run.result.created_at.isoformat()
    return windows

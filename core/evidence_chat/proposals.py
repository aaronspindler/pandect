"""Review one local PDF extraction and resume its original question exactly once."""

from functools import reduce
from operator import or_
from uuid import uuid4

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Value
from django.db.models.functions import Lower, StrIndex
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.crypto import constant_time_compare

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
from core.analysis.services import snapshot_sources, start_run
from core.case_search.indexing import authorize
from core.evidence.queries import scoped_evidence
from core.models import ChatAnalysisProposal, ChatTurn
from core.parser_contract import MAX_BYTES
from core.storage import private_file

from . import activity, ai, corpus, engine
from .corpus_search import terms_for
from .search_budget import reserve_lookup, timed_query


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
        case = authorize(parent.asked_by_id, parent.case_id, edit=True)
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


def _passages(case, run, question):
    """Read at most four short passages from this specific retained extraction."""
    from .passage_windows import lexical_spans, window_offsets

    rows = corpus.rows(case, "passage", "", False).filter(
        result__run=run, kind="document"
    )
    terms = terms_for(question)
    if terms:
        query = reduce(or_, (SearchQuery(t, config="simple") for t in terms))
        rows = rows.alias(
            relevance=SearchRank(SearchVector("text", config="simple"), query)
        ).order_by("-relevance", "item_index", "pk")
    else:
        rows = rows.order_by("item_index", "pk")
    # The query is already confined to the one PDF (at most 100 pages).
    positions = {
        f"pos_{n}": StrIndex(Lower("text"), Value(term)) for n, term in enumerate(terms)
    }
    windows = []
    for item in corpus.clipped(rows.annotate(**positions), "text")[:4]:
        hits = [(term, getattr(item, f"pos_{n}") - 1) for n, term in enumerate(terms)]
        spans = lexical_spans(item, "text", terms, hits)
        offset = window_offsets(spans)[0] if spans else 0
        sampled = corpus.clipped(rows.filter(pk=item.pk), "text", offset, 900).first()
        material = sampled and corpus.material(
            case, "passage", "document", sampled, False, offset, 900
        )
        if material and material.text:
            windows.append(engine.Window(material.locator, offset, material.text))
    return windows


def _answer_from_run(turn, run, context, check):
    reserve_lookup(turn)
    with timed_query():
        windows = _passages(turn.case, run, turn.question)
    activity.record(turn, "read_extraction", windows)
    if not windows:
        status = {
            "unsupported": "The document format was unsupported by this extraction.",
            "partial": "The partial extraction contains no readable passages.",
        }.get(
            run.status, "No readable passages from this extraction could be verified."
        )
        return engine.empty_answer(status), context
    result, used = engine.representative_answer(
        turn, check, turn.question, (windows + context)[:10]
    )
    if run.status == "partial":
        result["summary"] = "The document extraction has partial coverage."
    return result, used


def evaluate(turn, decision, windows, *, remaining):
    """Resolve a typed request against one numbered source. Never start work here."""
    loc = decision["locator"]
    try:
        doc = _document(turn.case, loc["pk"])
    except AnalysisError:
        result = decision["answer"]
        result["summary"] = "This source is ineligible for a PDF text extraction."
        return result, windows
    version = get_operation("native_text").version
    snapshots = snapshot_sources(turn.case, _selection(doc))
    run = find_extraction(turn.case_id, snapshots, version, published=True)
    if run and run.status in PUBLISHED:
        from .jobs import _authorize

        turn.plan["saved_extraction"] = str(run.pk)
        if remaining or run.status in ("empty", "unsupported", "insufficient"):
            return _answer_from_run(
                turn,
                run,
                windows,
                lambda selected, **kw: _authorize(turn, selected, **kw),
            )
        result = decision["answer"]
        result["summary"] = (
            "A saved extraction is available. The lookup limit was reached."
        )
        return result, windows
    turn.plan["proposed_extraction"] = {
        "document": str(doc.pk),
        "sha256": doc.sha256,
        "method_version": version,
        "purpose": decision["purpose"],
        "context": [[w.locator["key"], w.start] for w in windows],
    }
    return decision["answer"], windows


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
    case = authorize(actor.pk, case_id, edit=True, lock=True)
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
    from .jobs import enqueue_turn

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
    latest = turns.order_by("-ordinal").values_list("ordinal", flat=True).first()
    child = ChatTurn.objects.create(
        chat_id=chat_id,
        case_id=case_id,
        ordinal=latest + 1,
        asked_by_id=parent.asked_by_id,
        submission_id=uuid4(),
        question=parent.question,
        execution=parent.execution,
        approval_revision=parent.approval_revision,
        plan={
            "approved_proposal": str(ready.pk),
            "stage": "approved_extraction",
            "timezone": parent.plan.get("timezone", "UTC"),
        },
    )
    ready.continuation = child
    ready.save(update_fields=["continuation"])
    enqueue_turn(child)


def check_continuation(turn):
    proposal = (
        ChatAnalysisProposal.objects.select_related("parent__chat", "run")
        .filter(
            continuation=turn,
            parent__case_id=turn.case_id,
            parent__chat_id=turn.chat_id,
            parent__asked_by_id=turn.asked_by_id,
            run__case_id=turn.case_id,
            status="approved",
            run__result__case_id=turn.case_id,
        )
        .first()
    )
    if proposal is None or proposal.run.status not in PUBLISHED:
        raise AnalysisError("source_changed")
    document = check_offer(proposal)
    if (
        not equivalent_extractions(
            turn.case_id,
            snapshot_sources(turn.case, _selection(document)),
            proposal.method_version,
        )
        .filter(pk=proposal.run_id)
        .exists()
    ):
        raise AnalysisError("source_changed")
    return proposal


def finish_continuation(turn):
    from .jobs import _authorize, _finish_answer

    proposal = check_continuation(turn)
    refs = dict(
        proposal.parent.sources.filter(case_id=turn.case_id).values_list(
            "key", "locator"
        )
    )
    context = []
    for key, start in proposal.context:
        loc = refs.get(key)
        passage = loc and corpus.read_window(turn.case, loc, start, 900)
        if passage is None:
            raise AnalysisError("source_changed")
        context.append(engine.Window(loc, start, passage["text"]))
    result, used = _answer_from_run(
        turn,
        proposal.run,
        context,
        lambda windows, **kw: _authorize(turn, windows, **kw),
    )
    _finish_answer(turn, result, used)

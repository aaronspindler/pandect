"""Authorize, save and continue one delivery against current case evidence."""

from functools import partial
from hashlib import sha256
from uuid import uuid4

from django.utils import timezone

from cases.models import CaseActivity
from cases.permissions import get_case_for_verified_user
from core.analysis.errors import AnalysisError
from core.analysis.inference_payloads import encode_json
from core.models import ChatSource, ChatTurn

from . import activity, ai, corpus
from .history import read_context_window, reply_context
from .search_scope import context_source_allowed, question_filters, review_restart_code


def enqueue_turn(turn):
    from .tasks import process_chat_turn

    process_chat_turn.enqueue(str(turn.pk), str(turn.delivery_token))


def authorize_turn(turn, windows=(), *, lock=False, metadata=False, review=False):
    case = get_case_for_verified_user(
        turn.asked_by_id, turn.case_id, edit=True, lock=lock
    )
    if not ChatTurn.objects.filter(
        pk=turn.pk,
        case=case,
        chat__case=case,
        delivery_token=turn.delivery_token,
        status="running",
    ).exists():
        raise AnalysisError("processing_cancelled")
    if turn.include_archived:
        raise AnalysisError("chat_archive_scope_retired")
    if code := review_restart_code(turn):
        raise AnalysisError(code)
    ai.check_execution(turn, case)
    if turn.plan.get("approved_proposal"):
        from .proposals import check_continuation

        check_continuation(turn)
    if turn.plan.get("graph_checks"):
        from .investigation import current_leads

        current_leads(turn, case)
    if metadata:
        return case
    if digest := turn.plan.get("history_digest"):
        if sha256(encode_json(reply_context(turn))).hexdigest() != digest:
            raise AnalysisError("source_changed")
    if versions := turn.plan.get("review_question_sources"):
        # A resolved follow-up can contain evidence from a different review
        # batch. Its sources must still be available whenever that wording is used.
        locators = dict(
            turn.sources.filter(case=case, key__in=versions).values_list(
                "key", "locator"
            )
        )
        read = partial(read_context_window, turn)
        if locators.keys() != versions.keys() or any(
            locator["version"] != versions[key]
            or read(
                case,
                locator,
                0,
                1,
                include_archived=turn.include_archived,
            )
            is None
            for key, locator in locators.items()
        ):
            raise AnalysisError("source_changed")
    filters = question_filters(turn)
    for window in windows:
        if not context_source_allowed(case, window.locator, filters):
            raise AnalysisError("source_changed")
        current = corpus.read_window(
            case,
            window.locator,
            window.start,
            max(1, len(window.text)),
            include_archived=turn.include_archived,
            review=review,
        )
        if current is None or current["text"] != window.text:
            raise AnalysisError("source_changed")
    return case


def source_row(turn, locator, status):
    return ChatSource(
        case_id=turn.case_id,
        turn=turn,
        key=locator["key"],
        record_key=locator["record_key"],
        locator=locator,
        evidence_kind=locator["kind"],
        evidence_id=locator["pk"]
        if locator["surface"] == "trip"
        else locator.get("evidence_id"),
        result_id=locator.get("result_id"),
        status=status,
        gap=locator.get("gap", ""),
    )


def store_context(turn, windows):
    ChatSource.objects.bulk_create(
        [source_row(turn, w.locator, "context") for w in windows], ignore_conflicts=True
    )


def complete_turn(turn, result):
    from .proposals import record_proposal, resume_idle_chat

    record_proposal(turn)
    turn.plan.pop("context_bodies", None)
    activity.keep_ledger_sample(turn)
    turn.answer = result
    turn.status = "complete"
    turn.completed_at = timezone.now()
    turn.save(
        update_fields=(
            "answer",
            "status",
            "completed_at",
            "mode",
            "total",
            "reviewed",
            "gaps",
            "plan",
            "activity",
        )
    )
    resume_idle_chat(turn.case_id, turn.chat_id)


def continue_turn(turn):
    turn.delivery_token = uuid4()
    turn.save(
        update_fields=("delivery_token", "reviewed", "gaps", "plan", "total", "mode")
    )
    enqueue_turn(turn)


def clear_lookup(turn):
    for key in (
        "refinement",
        "refine_query",
        "refine_windows",
        "search_queries",
        "lookups",
    ):
        turn.plan.pop(key, None)


def evidence_versions(case, *, lock=False):
    # Work advances with each chat delivery and is intentionally excluded.
    rows = CaseActivity.objects.filter(case=case)
    if lock:
        rows = rows.select_for_update()
    return rows.values("evidence", "findings", "people", "vehicles", "images").get()

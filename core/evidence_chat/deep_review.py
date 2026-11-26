"""Approve a metered interpretive review of a prior answer's defined population."""

from math import ceil
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import BigIntegerField, Count, F, Sum
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Ceil, Greatest, Length
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.crypto import constant_time_compare

from core.analysis.errors import AnalysisError
from core.analysis.execution import execution_confirmation
from core.case_search.indexing import authorize
from core.models import ChatTurn

from . import ai, corpus, jobs
from .corpus_search import matching_rows, terms_for
from .sampling import topic_query
from .search_budget import timed_query
from .search_scope import (
    DATE,
    QUOTED_TEXT,
    TYPE,
    normalize_date_filters,
    question_filters,
    scope_rows,
)

MAX_ENTRIES = 100_000
MAX_REQUESTS = 10_000
# In a question with a verified population these request words describe how
# to interpret it, rather than additional terms its members must contain.
INTERPRETIVE_WORDS = frozenset(
    "why compare explain differ differs differed different differences "
    "mean means text texts message messages record contain contains containing "
    "mentioning saying wording".split()
)


def offer(turn):
    """Rendering an ordinary reply never enumerates case evidence."""
    if turn.review_of_id:
        return turn.status == "complete" and bool(turn.plan.get("unreviewed"))
    if turn.mode != "ordinary" or not turn.started_at:
        return False
    selection = turn.plan.get("recorded_selection", {})
    return bool(
        selection.get("snapshot")
        or turn.plan.get("ranked_scope_limited")
        or turn.plan.get("candidate_limit_reached")
        or (turn.answer.get("lead") and turn.total)
    )


def _scope(turn):
    selection = turn.plan.get("recorded_selection", {})
    snapshot = selection.get("snapshot")
    question = turn.question
    within = None
    phrase = selection.get("phrase", "") if selection.get("context_only") else ""
    if phrase:
        # The exact phrase already selects the originals. Its words cannot also
        # count as evidence of a separate subject in the investigator's question.
        question = QUOTED_TEXT.sub(" ", question)
    if selection.get("count_day"):
        question = normalize_date_filters(question)
    terms = terms_for(
        TYPE.sub("", DATE.sub("", question)),
        extra_stopwords=INTERPRETIVE_WORDS if selection.get("context_only") else (),
    )
    if snapshot and selection.get("context_only") and terms:
        # Type/date alone establishes availability, not topical relevance.
        # Literal and recorded event-time predicates require a verified original;
        # an upload timestamp cannot substitute for a missing event timestamp.
        within = snapshot if phrase or selection.get("count_day") else None
        snapshot = None
    filters = {
        key: value.isoformat() if key in ("start", "end") else value
        for key, value in question_filters(turn).items()
    }
    if selection.get("count_day"):
        filters.update(
            {key: selection[key] for key in ("start", "end") if key in selection}
        )
    if selection.get("context_only") and len(kinds := selection.get("kinds", [])) == 1:
        filters.setdefault("record_type", kinds[0])
    return {
        "snapshot": snapshot,
        "within_snapshot": within,
        "phrase": phrase,
        "terms": [] if snapshot else terms,
        "filters": {} if snapshot else filters,
        "message_topics": [] if snapshot else turn.plan.get("message_topics", []),
    }


def _rows(turn, scope):
    """Use one declared predicate for the estimate and worker inventory."""
    if scope["snapshot"] or scope["within_snapshot"]:
        from .selections import member_ids

        members = member_ids(turn)
        if members is None:
            raise AnalysisError("source_changed")
        for kind in members.order_by("kind").values_list("kind", flat=True).distinct():
            rows = corpus.rows(
                turn.case, "original", kind, False, through=turn.started_at
            ).filter(pk__in=members.filter(kind=kind).values("evidence_id"))
            if scope["within_snapshot"]:
                rows = _lexical_rows(rows, "original", kind, scope)
            yield "original", kind, rows
        return
    if not scope["terms"]:
        return
    for surface, kind in corpus.surfaces():
        rows = scope_rows(
            corpus.rows(turn.case, surface, kind, False, through=turn.started_at),
            turn.case,
            surface,
            kind,
            scope["filters"],
        )
        yield surface, kind, _lexical_rows(rows, surface, kind, scope)


def _lexical_rows(rows, surface, kind, scope):
    if surface == "original" and kind == "text-message" and scope["message_topics"]:
        rows = rows.filter(search_words=topic_query(scope["message_topics"]))
    return matching_rows(rows, surface, kind, scope["terms"], None).order_by("pk")


def check_parent(turn):
    from .services import inspect_turn

    case = authorize(turn.asked_by_id, turn.case_id, edit=True)
    if not offer(turn) or turn.chat.case_id != case.pk or turn.status != "complete":
        raise Http404
    if turn.review_of_id:
        parent_for(turn)
    if inspect_turn(turn) is None:
        raise AnalysisError("source_changed")
    if ai.turn_execution(case) != (turn.execution, turn.approval_revision):
        raise AnalysisError("execution_changed")


def _workload(rows, length=None):
    # Full review overlaps adjacent excerpts. Estimate in SQL so evaluating the
    # approval page does not transfer the matched bodies or allocate a manifest.
    if length is None:
        count = rows.count()
        return count, count
    windows = Greatest(
        1.0, Ceil((length - jobs.OVERLAP) * 1.0 / (jobs.WINDOW - jobs.OVERLAP))
    )
    totals = rows.aggregate(entries=Count("pk"), windows=Sum(windows, default=0))
    return totals["entries"], int(totals["windows"])


def estimate(parent, actor):
    check_parent(parent)
    if parent.review_of_id:
        scope = parent.plan["deep_scope"].copy()
        count, windows = _workload(
            parent.sources.filter(case=parent.case, status="unread"),
            Cast(KT("locator__length"), BigIntegerField()) - F("cursor"),
        )
        scope["resume"] = True
    else:
        scope = _scope(parent)
        scope.update(
            versions={} if scope["snapshot"] else jobs._versions(parent.case),
            through=parent.started_at.isoformat(),
            requested_by=str(parent.asked_by_id),
        )
        count = windows = 0
        with timed_query():
            for surface, kind, rows in _rows(parent, scope):
                body = corpus.body_field(surface, kind)
                source_count, source_windows = _workload(
                    rows, Length(body) if body else None
                )
                count += source_count
                windows += source_windows
    if not count:
        raise ValidationError("No active sources match the proposed review scope.")
    if count > MAX_ENTRIES:
        raise ValidationError(
            f"The selection exceeds {MAX_ENTRIES:,} entries. "
            "Narrow the question by date, wording or evidence type."
        )
    # Four short excerpts fit per classification call. Long records, a repeated
    # transport attempt or a failed delivery may use more of the approved limit.
    expected = ceil(windows / jobs.REVIEW_BATCH) + 1
    scope.update(count=count, limit=MAX_REQUESTS)
    token = execution_confirmation(
        "chat.approve.deeper-review",
        [
            str(actor.pk),
            str(parent.pk),
            parent.approval_revision,
            parent.started_at.isoformat(),
            scope,
        ],
        parent.execution,
    )
    return scope, expected, token


@transaction.atomic
def approve(actor, case_id, chat_id, parent_id, question, token, request_limit):
    from .services import _require_idle

    case = authorize(actor.pk, case_id, edit=True, lock=True)
    parent = get_object_or_404(
        ChatTurn.objects.select_related("case", "chat"),
        pk=parent_id,
        case=case,
        chat_id=chat_id,
        chat__case=case,
    )
    existing = ChatTurn.objects.filter(
        review_of=parent, case=case, chat_id=chat_id
    ).first()
    if existing:
        return existing
    scope, _, signature = estimate(parent, actor)
    if not constant_time_compare(token, signature):
        raise ValidationError(
            "The scope or processing settings changed. Review the current estimate."
        )
    if (
        not 1 <= request_limit <= scope["limit"]
        or not 1 <= len(question.strip()) <= 2000
    ):
        raise ValidationError(
            "Enter a review question and a request limit within the estimate."
        )
    if scope.get("resume") and question.strip() != parent.question:
        raise ValidationError("Continue with the question used for the earlier review.")
    _require_idle(parent.chat)
    ordinal = (
        parent.chat.turns.filter(case=case)
        .order_by("-ordinal")
        .values_list("ordinal", flat=True)
        .first()
    )
    child = ChatTurn.objects.create(
        case=case,
        chat=parent.chat,
        review_of=parent,
        ordinal=ordinal + 1,
        asked_by=actor,
        question=question.strip(),
        submission_id=uuid4(),
        mode="full",
        execution=parent.execution,
        approval_revision=parent.approval_revision,
        plan={
            "stage": "prepare_deeper_review",
            "deep_scope": scope | {"limit": request_limit},
            "review_version": jobs.FULL_REVIEW_VERSION,
            "timezone": parent.plan.get("timezone", "UTC"),
        },
    )
    jobs.enqueue_turn(child)
    return child


def parent_for(turn):
    parent = (
        ChatTurn.objects.only("asked_by_id")
        .filter(
            pk=turn.review_of_id,
            case_id=turn.case_id,
            chat_id=turn.chat_id,
            chat__case_id=turn.case_id,
            status="complete",
            ordinal__lt=turn.ordinal,
        )
        .first()
    )
    if parent is None:
        raise AnalysisError("source_changed")
    for actor in {str(parent.asked_by_id), turn.plan["deep_scope"]["requested_by"]} - {
        str(turn.asked_by_id)
    }:
        authorize(actor, turn.case_id, edit=True)
    return parent


def manifest_rows(turn):
    parent = parent_for(turn)
    scope = turn.plan["deep_scope"]
    check_parent(parent)
    if scope.get("resume"):
        for locator, cursor in (
            parent.sources.filter(case=turn.case, status="unread")
            .values_list("locator", "cursor")
            .iterator(chunk_size=200)
        ):
            source = jobs._row(turn, locator, "pending")
            source.cursor = cursor
            yield source
        return
    if (
        (scope["versions"] and jobs._versions(turn.case) != scope["versions"])
        or parent.started_at.isoformat() != scope["through"]
        or any(scope.get(key) != value for key, value in _scope(parent).items())
    ):
        raise AnalysisError("source_changed")
    for locator in corpus.discover(
        turn.case,
        False,
        through=parent.started_at,
        review=True,
        selected_rows=_rows(parent, scope),
    ):
        yield jobs._row(turn, locator, "pending")


def reserve_request(turn, purpose):
    """Apply the approved cap across dispatch, retries and recovery."""
    limit = turn.plan["deep_scope"]["limit"]
    available = ChatTurn.objects.filter(
        pk=turn.pk,
        case_id=turn.case_id,
        chat__case_id=turn.case_id,
        delivery_token=turn.delivery_token,
        status="running",
        review_requests__lt=limit,
    )
    if purpose == "answer":
        allowed = available.update(review_requests=F("review_requests") + 1)
    else:
        # Local prompt rendering precedes generation and carries the same material.
        allowed = available.exists()
    if not allowed:
        raise AnalysisError("chat_deeper_review_limit")


@transaction.atomic
def finish_at_limit(turn):
    case = jobs._authorize(turn, lock=True)
    unread = turn.sources.filter(case=case, status__in=jobs.PENDING_REVIEW)
    remaining = unread.update(
        status="unread",
        gap="This entry was unfinished at the approved generation limit.",
    )
    turn.gaps += remaining
    turn.plan.update(stage="verify", unreviewed=remaining, approved_limit_reached=True)
    jobs._continue(turn)

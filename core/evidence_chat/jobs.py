"""Checkpoint case chat reviews and publish only validated, complete answers."""

import logging
from hashlib import sha256
from itertools import batched
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone

from cases.models import CaseActivity
from core.analysis.errors import AnalysisError
from core.analysis.inference_payloads import encode_json
from core.case_search.indexing import authorize
from core.models import ChatMatch, ChatSource, ChatTurn

from . import ai, corpus, engine, engine_counts

logger = logging.getLogger(__name__)

WINDOW = 900
OVERLAP = 120
REVIEW_BATCH = 4
STRUCTURED_BATCH = 64


def enqueue_turn(turn):
    from .tasks import process_chat_turn

    process_chat_turn.enqueue(str(turn.pk), str(turn.delivery_token))


def _authorize(turn, windows=(), *, lock=False):
    case = authorize(turn.asked_by_id, turn.case_id, edit=True, lock=lock)
    if not ChatTurn.objects.filter(
        pk=turn.pk,
        case=case,
        chat__case=case,
        delivery_token=turn.delivery_token,
        status="running",
    ).exists():
        raise AnalysisError("processing_cancelled")
    ai.check_execution(turn, case)
    if digest := turn.plan.get("history_digest"):
        if sha256(encode_json(_history(turn))).hexdigest() != digest:
            raise AnalysisError("source_changed")
    for window in windows:
        current = corpus.read_window(
            case,
            window.locator,
            window.start,
            max(1, len(window.text)),
            include_archived=turn.include_archived,
        )
        if current is None or current["text"] != window.text:
            raise AnalysisError("source_changed")
    return case


@transaction.atomic
def _claim(turn_id, token):
    turn = (
        ChatTurn.objects.select_for_update(of=("self",))
        .select_related("case", "asked_by")
        .filter(pk=turn_id, delivery_token=token, status__in=("queued", "running"))
        .first()
    )
    if turn is not None:
        turn.delivery_token = uuid4()
        turn.status = "running"
        turn.started_at = turn.started_at or timezone.now()
        turn.save(update_fields=("delivery_token", "status", "started_at"))
    return turn


def _full_reply_context(previous, turn):
    """Resolve a small sample in the same order as the exhaustive match list."""
    sources = previous.sources.filter(case_id=turn.case_id, status="match").order_by(
        "record_key", "key"
    )[:6]
    excerpts = []
    position = 0
    for source in sources:
        hits = source.hits.filter(turn=previous, case_id=turn.case_id).order_by(
            "start", "end"
        )[: 6 - position]
        for hit in hits:
            position += 1
            ref = corpus.cite(
                turn.case,
                source.locator,
                hit.start,
                hit.quote,
                include_archived=turn.include_archived,
            )
            if ref:
                excerpts.append(
                    f"Match {position} · {ref['label'][:100]} · {ref['quote'][:150]}"
                )
        if position == 6:
            break
    return "\n".join(excerpts)[:600]


def _history(turn):
    from .services import inspect_turn

    rows = ChatTurn.objects.filter(
        chat=turn.chat_id,
        case=turn.case_id,
        ordinal__lt=turn.ordinal,
        status="complete",
    ).order_by("-ordinal")[:3]
    history = []
    for previous in reversed(list(rows)):
        if previous.mode == "full":
            history.append(
                {
                    "question": previous.question[:300],
                    "reply_context": _full_reply_context(previous, turn),
                }
            )
            continue
        answer = inspect_turn(previous)
        if answer is None:
            continue
        topic = answer.get("clarification") or " ".join(
            x["text"] for x in answer.get("claims", [])[:2]
        )
        # Respect the new question's archive scope before reusing old wording.
        if (
            topic
            and not turn.include_archived
            and any(
                corpus.read_window(turn.case, source.locator, 0, 1) is None
                for source in previous.sources.filter(case=turn.case).iterator()
            )
        ):
            topic = ""
        history.append(
            {"question": previous.question[:300], "reply_context": topic[:400]}
        )
    return history


def _rank(turn, query, limit):
    found = corpus.rank(
        turn.case,
        query,
        user=turn.asked_by,
        include_archived=turn.include_archived,
        limit=limit,
        through=turn.started_at,
    )
    windows = []
    for locator, start in found:
        row = corpus.read_window(
            turn.case, locator, start, WINDOW, include_archived=turn.include_archived
        )
        if row and row["text"]:
            windows.append(engine.Window(locator, start, row["text"]))
    return windows


def _row(turn, locator, status):
    return ChatSource(
        case_id=turn.case_id,
        turn=turn,
        key=locator["key"],
        record_key=locator["record_key"],
        locator=locator,
        evidence_kind=locator["kind"],
        evidence_id=locator.get("evidence_id"),
        result_id=locator.get("result_id"),
        status=status,
        gap=locator.get("gap", ""),
    )


def _store_context(turn, windows):
    ChatSource.objects.bulk_create(
        [_row(turn, w.locator, "context") for w in windows], ignore_conflicts=True
    )


def _complete(turn, result):
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
        )
    )


@transaction.atomic
def _publish_answer(turn, result, windows):
    case = _authorize(turn, windows, lock=True)
    _store_context(turn, windows)
    known = {w.locator["key"]: w.locator for w in windows}
    for claim in result["claims"]:
        for cite in claim["citations"]:
            if not corpus.cite(
                case,
                known[cite["key"]],
                cite["start"],
                cite["quote"],
                include_archived=turn.include_archived,
            ):
                raise AnalysisError("source_changed")
    turn.mode = "clarification" if result["clarification"] else "ordinary"
    entries = turn.sources.filter(case=case)
    turn.total = turn.reviewed = entries.count()
    turn.gaps = entries.exclude(gap="").count()
    _complete(turn, result)


def _continue(turn):
    turn.delivery_token = uuid4()
    turn.save(
        update_fields=("delivery_token", "reviewed", "gaps", "plan", "total", "mode")
    )
    enqueue_turn(turn)


def _freeze(turn, question, filters, context=(), *, narrative=False):
    # Persist one consistent database snapshot, streaming only identities and
    # digests into the manifest. Later tasks resolve bounded content windows.
    nested = connection.in_atomic_block
    with transaction.atomic():
        if not nested:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        case = _authorize(turn, context)
        _store_context(turn, context)
        rows = (
            _row(turn, locator, "pending")
            for locator in corpus.discover(
                case,
                turn.include_archived,
                through=turn.started_at,
                original_kinds=filters.get("kinds"),
            )
        )
        for batch in batched(rows, 200, strict=False):
            ChatSource.objects.bulk_create(
                batch,
                update_conflicts=True,
                update_fields=("status", "locator", "gap", "result_id"),
                unique_fields=("turn", "key"),
            )
        # This shares the publication lock with evidence deletion. The repeatable
        # read transaction must retry if another writer advanced the case activity
        # row after our snapshot, including a deletion whose inventory missed us.
        _authorize(turn, lock=True)
        CaseActivity.objects.select_for_update().get(case=case)
        turn.mode = "full"
        turn.plan = turn.plan | {
            "stage": "review",
            "question": question,
            "filters": filters,
            "narrative": narrative,
            "review_kind": "structured" if filters else "interpretive",
        }
        turn.total = turn.sources.filter(case=case, status="pending").count()
        _continue(turn)


def _decide(turn):
    filters, clarification = engine_counts.structured_question(turn, turn.question)
    if clarification:
        _publish_answer(turn, engine.empty_answer(clarification=clarification), [])
        return
    if filters:
        _freeze(turn, turn.question, filters)
        return
    history = _history(turn)
    search = turn.question
    if history:
        turn.plan["history_digest"] = sha256(encode_json(history)).hexdigest()
        search += " " + history[-1]["question"] + " " + history[-1]["reply_context"]
    preview = _rank(turn, search, 4)

    def authorize_context(windows, **kwargs):
        return _authorize(turn, windows, **kwargs)

    action, question, preview = engine.resolve_question(
        turn, authorize_context, history, preview
    )
    if action == "ask":
        _publish_answer(turn, engine.empty_answer(clarification=question), preview)
    elif action in ("review", "review_answer"):
        # Only exact investigator wording may qualify for a deterministic total.
        _freeze(turn, question, {}, preview, narrative=action == "review_answer")
    else:
        windows = _rank(turn, question, 10)
        result, used = engine.answer(turn, authorize_context, question, windows)
        _publish_answer(turn, result, preview + used)


def _checked_review(turn, windows):
    try:
        return engine.review(
            turn,
            lambda fragments, **kw: _authorize(turn, fragments, **kw),
            turn.plan["question"],
            windows,
        )
    except AnalysisError as error:
        if error.code not in (
            "model_content_limit",
            "local_ai_context_limit",
            "local_ai_input_limit",
            "gateway_input_limit",
        ):
            raise
        if len(windows) == 1:
            return [("uncertain", [])]
        split = len(windows) // 2
        return _checked_review(turn, windows[:split]) + _checked_review(
            turn, windows[split:]
        )


def _review_batch(turn):
    filters = turn.plan["filters"]
    pending = list(
        turn.sources.filter(case=turn.case_id, status="pending").order_by("pk")[
            : STRUCTURED_BATCH if filters else REVIEW_BATCH
        ]
    )
    if not pending:
        if turn.plan.get("narrative"):
            _draft_review(turn)
        _verify_batch(turn)
        return
    work = []
    readable = []
    for source in pending:
        gap = "" if filters else source.gap
        start = source.locator.get("metadata_start", 0) if filters else source.cursor
        piece = corpus.read_window(
            turn.case,
            source.locator,
            start,
            1200 if filters else WINDOW,
            include_archived=turn.include_archived,
        )
        window = None
        if piece and piece["text"]:
            window = engine.Window(source.locator, start, piece["text"])
            readable.append(window)
        elif piece is None:
            gap = "This source version could no longer be read."
        work.append((source, window, gap))
    outcomes = []
    if filters:
        evaluated = engine_counts.evaluate(turn.case, pending, filters)
        for source, w, _ in work:
            if w:
                outcome = evaluated.get(source.evidence_id, "miss")
                citations = []
                if outcome == "match":
                    citations.append(
                        {
                            "start": w.start,
                            "end": w.start + len(w.text),
                            "quote": w.text,
                        }
                    )
                outcomes.append((outcome, citations))
    elif readable:
        outcomes = _checked_review(turn, readable)

    with transaction.atomic():
        # Permission and every excerpt are rechecked under the case lock;
        # stale deliveries cannot advance a cursor or publish any match.
        case = _authorize(turn, readable, lock=True)
        verified = iter(outcomes)
        hits = []
        for source, w, gap in work:
            outcome, refs = next(verified) if w else ("miss", [])
            if outcome == "uncertain":
                gap = "Some material could not be interpreted for this question."
            source.gap = gap
            for ref in refs:
                hits.append(
                    ChatMatch(
                        case=case,
                        turn=turn,
                        source=source,
                        start=ref["start"],
                        end=ref["end"],
                        quote=ref["quote"],
                    )
                )
            consumed = source.cursor + len(w.text) if w else source.cursor
            if not filters and w and consumed < source.locator["length"]:
                source.cursor = max(source.cursor + 1, consumed - OVERLAP)
            else:
                source.cursor = consumed
                source.status = (
                    "match"
                    if refs or source.hits.exists()
                    else "gap"
                    if gap
                    else "miss"
                )
                turn.reviewed += 1
                turn.gaps += bool(gap)
        ChatMatch.objects.bulk_create(hits, ignore_conflicts=True)
        ChatSource.objects.bulk_update(pending, ("status", "gap", "cursor"))
        _continue(turn)


def _draft_review(turn):
    """Give a narrative request a cited answer and retain the entire match ledger."""
    examples = (
        turn.hits.filter(
            case_id=turn.case_id, source__turn=turn, source__case_id=turn.case_id
        )
        .select_related("source")
        .order_by("source__record_key", "start", "pk")
        .distinct("source__record_key")[:8]
    )
    windows = [
        engine.Window(hit.source.locator, hit.start, hit.quote) for hit in examples
    ]
    if windows:

        def authorize_examples(fragments, **kwargs):
            return _authorize(turn, fragments, **kwargs)

        draft, used = engine.answer(
            turn,
            authorize_examples,
            turn.plan["question"],
            windows,
            representative=True,
        )
        _authorize(turn, used)
        turn.plan["draft"] = draft
        turn.plan["examples"] = len(used)


def _versions(case, *, lock=False):
    # Work advances with each chat delivery and is intentionally excluded.
    rows = CaseActivity.objects.filter(case=case)
    if lock:
        rows = rows.select_for_update()
    return rows.values("evidence", "findings", "people", "vehicles", "images").get()


def _verify_batch(turn):
    """Verify outside the case lock and pin the revisions at publication."""
    case = _authorize(turn)
    versions = _versions(case)
    accounted = turn.sources.filter(case=case).exclude(status="context")
    candidates = accounted.order_by("pk")
    cursor = turn.plan.get("verified_through")
    if cursor and turn.plan.get("verify_versions") == versions:
        candidates = candidates.filter(pk__gt=cursor)
    page = list(candidates[:STRUCTURED_BATCH])
    for source in page:
        valid = corpus.read_window(
            case, source.locator, 0, 1, include_archived=turn.include_archived
        )
        if valid is None and source.status != "gap":
            raise AnalysisError("source_changed")
    with transaction.atomic():
        _authorize(turn, lock=True)
        current_versions = _versions(case, lock=True)
        turn.plan["stage"] = "verify"
        turn.plan["verify_versions"] = current_versions
        if current_versions != versions:
            turn.plan.pop("verified_through", None)
            _continue(turn)
        elif len(page) == STRUCTURED_BATCH:
            turn.plan["verified_through"] = str(page[-1].pk)
            _continue(turn)
        else:
            _finish_review(turn, accounted)


def _finish_review(turn, accounted):
    if turn.reviewed != turn.total or accounted.filter(status="pending").exists():
        raise AnalysisError("source_changed")
    records = accounted.filter(status="match").values("record_key").distinct().count()
    turn.plan["matched_records"] = records
    turn.plan["stage"] = "complete"
    total_records = accounted.values("record_key").distinct().count()
    if turn.plan["filters"]:
        summary = f"{records} matching records in the reviewed scope."
    else:
        summary = (
            f"The review identified {records} distinct matching records "
            f"among {total_records} reviewed."
        )
    if turn.gaps:
        summary += f" Coverage is incomplete for {turn.gaps} source entries."
    lead = "Review the sources with incomplete coverage." if turn.gaps else ""
    draft = turn.plan.pop("draft", None) or engine.empty_answer()
    if n := turn.plan.get("examples"):
        summary += f" The narrative uses {n} representative matching records."
    draft["summary"] = summary
    draft["lead"] = draft.get("lead") or lead
    _complete(turn, draft)


def process_turn(turn_id, token):
    turn = _claim(turn_id, token)
    if turn is None:
        return
    active_token = turn.delivery_token
    try:
        _authorize(turn)
        if turn.plan.get("stage") == "verify":
            _verify_batch(turn)
        elif turn.plan.get("stage") == "review":
            _review_batch(turn)
        else:
            _decide(turn)
    except Http404, PermissionDenied:
        _fail(turn, active_token, "access_removed")
    except AnalysisError as error:
        _fail(turn, active_token, error.code)
    except Exception as error:
        # Preserve a retryable domain record without retaining provider output
        # or exception text that could contain case evidence.
        _fail(turn, active_token, "processing_failed")
        logger.error(
            "Chat review failed turn=%s exception_type=%s",
            turn.pk,
            type(error).__name__,
        )


def _fail(turn, active_token, code):
    ChatTurn.objects.filter(
        pk=turn.pk, delivery_token=active_token, status="running"
    ).update(status="failed", error_code=code, completed_at=timezone.now())

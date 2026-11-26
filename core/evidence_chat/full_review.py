"""Freeze, inspect and verify an exhaustive Evidence Chat review."""

import re
from hashlib import sha256
from itertools import batched

from django.db import connection, transaction
from django.db.models import Count

from cases.models import CaseActivity
from core.analysis.errors import AnalysisError
from core.analysis.inference_payloads import encode_json
from core.models import ChatMatch, ChatSource

from . import activity, corpus, engine, engine_counts
from .checkpoints import (
    authorize_turn,
    clear_lookup,
    complete_turn,
    continue_turn,
    evidence_versions,
    source_row,
    store_context,
)
from .passage_windows import WINDOW
from .search_scope import FULL_REVIEW_VERSION, question_filters

OVERLAP = 120
REVIEW_BATCH = 4
STRUCTURED_BATCH = 64
LITERAL_CITATIONS_PER_RECORD = 4
PENDING_REVIEW = ("pending", "pending_hit")


def freeze_review(
    turn, question, filters, context=(), *, narrative=False, literal="", scope=None
):
    # Persist one consistent database snapshot, streaming only identities and
    # digests into the manifest. Later tasks resolve bounded content windows.
    activity.record(turn, "select")
    explicit_scope = question_filters(turn) if scope is None else scope
    nested = connection.in_atomic_block
    with transaction.atomic():
        if not nested:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        case = authorize_turn(turn, context)
        store_context(turn, context)
        rows = (
            source_row(turn, locator, "pending")
            for locator in corpus.discover(
                case,
                turn.include_archived,
                through=turn.started_at,
                original_kinds=filters.get("kinds"),
                count_filters=filters
                if engine_counts.is_filtered_count(question, filters)
                else None,
                review=True,
                filters=explicit_scope,
            )
        )
        for batch in batched(rows, 200, strict=False):
            ChatSource.objects.bulk_create(
                batch,
                update_conflicts=True,
                update_fields=("status", "locator", "gap", "result_id", "cursor"),
                unique_fields=("turn", "key"),
            )
        if context and question != turn.question:
            turn.plan["review_question_sources"] = {
                w.locator["key"]: w.locator["version"] for w in context
            }
        # This shares the publication lock with evidence deletion. The repeatable
        # read transaction must retry if another writer advanced the case activity
        # row after our snapshot, including a deletion whose inventory missed us.
        authorize_turn(turn, lock=True)
        CaseActivity.objects.select_for_update().get(case=case)
        turn.mode = "full"
        clear_lookup(turn)
        turn.plan = turn.plan | {
            "stage": "review",
            "review_version": FULL_REVIEW_VERSION,
            "question": question,
            "filters": filters,
            "narrative": narrative,
            "review_kind": "structured" if filters else "interpretive",
        }
        if literal:
            turn.plan.update(review_kind="literal", literal=literal)
        turn.total = turn.sources.filter(case=case, status="pending").count()
        continue_turn(turn)


def _checked_review(turn, windows, spans):
    try:
        return engine.review(
            turn,
            lambda _, **kw: authorize_turn(turn, spans, review=True, **kw),
            turn.plan["question"],
            windows,
        )
    except AnalysisError as error:
        if error.code not in (
            "model_content_limit",
            "gateway_input_limit",
        ):
            raise
        if len(windows) == 1:
            return [("uncertain", [])]
        split = len(windows) // 2
        return _checked_review(turn, windows[:split], spans) + _checked_review(
            turn, windows[split:], spans
        )


def _read_review_spans(turn, pending, filters, *, literal=False):
    """Read nearby excerpts once and retain their shared verification span."""
    work = []
    readable = []
    spans = []
    for source in pending:
        slots = REVIEW_BATCH - len(readable)
        if not filters and not slots:
            break
        gap = "" if filters else source.gap
        start = source.locator.get("metadata_start", 0) if filters else source.cursor
        piece = corpus.read_window(
            turn.case,
            source.locator,
            start,
            1200 if filters else WINDOW + (slots - 1) * (WINDOW - OVERLAP),
            include_archived=turn.include_archived,
            review=True,
            literal=literal,
        )
        windows = []
        if piece is None:
            gap = "This source version could no longer be read."
        elif text := piece["text"]:
            if "verified_body" in piece:
                source.locator["verified_body"] = piece["verified_body"]
            span = engine.Window(source.locator, start, text)
            spans.append(span)
            if filters:
                windows.append(span)
            else:
                # Read and hash one contiguous span per source while retaining
                # the provider's existing excerpt size and overlap.
                for offset in range(0, len(text), WINDOW - OVERLAP):
                    excerpt = text[offset : offset + WINDOW]
                    windows.append(
                        engine.Window(
                            source.locator,
                            start + offset,
                            excerpt,
                            literal_ranges=piece.get("literal_ranges", ()),
                        )
                    )
                    if offset + len(excerpt) == len(text):
                        break
            readable.extend(windows)
        work.append((source, windows, gap))
    return work, readable, spans


def _literal_batch(term, windows):
    records = [
        {
            "text": window.text,
            "start": window.start,
            "length": window.locator["length"],
            "ranges": window.literal_ranges,
        }
        for window in windows
    ]
    identities = [(w.locator["key"], w.locator["version"]) for w in windows]
    digest = sha256(encode_json([identities, records, term])).hexdigest()
    return {"literal": term, "batch": digest, "records": records}


def _literal_results(reply, batch, windows):
    if reply.get("batch") != batch["batch"]:
        raise AnalysisError("source_changed")
    rows = reply.get("offsets")
    if not isinstance(rows, list) or len(rows) != len(windows):
        raise AnalysisError("model_result_structure")
    term = batch["literal"]
    left = r"(?<!\w)" if re.match(r"\w", term[0]) else ""
    right = r"(?!\w)" if re.match(r"\w", term[-1]) else ""
    expected = re.compile(left + re.escape(term) + right, re.IGNORECASE)
    outcomes = []
    for window, offsets in zip(windows, rows, strict=True):
        if not isinstance(offsets, list) or len(offsets) > LITERAL_CITATIONS_PER_RECORD:
            raise AnalysisError("model_result_structure")
        citations = []
        for offset in offsets:
            if type(offset) is not int or not 0 <= offset < len(window.text):
                raise AnalysisError("model_quote_mismatch")
            # Verify only the exact position proposed by the Worker. The
            # overlap places clipped boundary matches inside an adjacent span.
            match = expected.match(window.text, offset)
            if not match or (left and offset == 0 and window.start):
                raise AnalysisError("model_quote_mismatch")
            start, end = window.start + offset, window.start + match.end()
            if (
                right
                and match.end() == len(window.text)
                and end < window.locator["length"]
            ):
                raise AnalysisError("model_quote_mismatch")
            if not any(
                first <= start and end <= last for first, last in window.literal_ranges
            ):
                raise AnalysisError("model_quote_mismatch")
            citations.append({"start": start, "end": end, "quote": match[0]})
        outcomes.append(("match" if citations else "miss", citations))
    return outcomes


def review_batch(turn, *, literal_result=None):
    filters = turn.plan["filters"]
    literal = turn.plan.get("literal")
    pending = list(
        turn.sources.filter(case=turn.case_id, status__in=PENDING_REVIEW).order_by(
            "pk"
        )[: STRUCTURED_BATCH if filters else REVIEW_BATCH]
    )
    if not pending:
        if turn.plan.get("narrative"):
            _draft_review(turn)
        verify_batch(turn)
        return
    work, readable, spans = _read_review_spans(
        turn, pending, filters, literal=bool(literal)
    )
    activity.record(turn, "review", spans)
    outcomes = []
    if filters:
        evaluated = engine_counts.evaluate(turn.case, pending, filters)
        for source, windows, _ in work:
            if windows:
                w = windows[0]
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
    elif literal and readable:
        batch = _literal_batch(literal, readable)
        if literal_result is None:
            authorize_turn(turn, spans, review=True)
            return batch
        outcomes = _literal_results(literal_result, batch, readable)
    elif literal_result is not None:
        raise AnalysisError("source_changed")
    elif readable:
        outcomes = _checked_review(turn, readable, spans)

    with transaction.atomic():
        # Permission and every excerpt are rechecked under the case lock;
        # stale deliveries cannot advance a cursor or publish any match.
        case = authorize_turn(turn, spans, lock=True, review=True)
        counts, seen = _literal_citations(turn, work) if literal else ({}, set())
        verified = iter(outcomes)
        hits = []
        for source, windows, gap in work:
            matched = source.status == "pending_hit"
            for _ in windows:
                outcome, refs = next(verified)
                matched |= outcome == "match"
                if outcome == "uncertain":
                    gap = "Some material could not be interpreted for this question."
                for ref in refs:
                    if literal:
                        key = source.pk, ref["start"], ref["end"]
                        count = counts.get(source.record_key, 0)
                        if count >= LITERAL_CITATIONS_PER_RECORD or key in seen:
                            continue
                        counts[source.record_key] = count + 1
                        seen.add(key)
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
            source.gap = gap
            consumed = source.cursor
            if windows:
                last = windows[-1]
                consumed = (source.cursor if filters else last.start) + len(last.text)
            if not filters and windows and consumed < source.locator["length"]:
                source.cursor = max(source.cursor + 1, consumed - OVERLAP)
                if literal and matched:
                    source.status = "pending_hit"
            else:
                source.cursor = consumed
                source.status = (
                    "match"
                    if matched or source.hits.filter(case=case, turn=turn).exists()
                    else "gap"
                    if gap
                    else "miss"
                )
                turn.reviewed += 1
                turn.gaps += bool(gap)
        ChatMatch.objects.bulk_create(hits, ignore_conflicts=True)
        ChatSource.objects.bulk_update(
            [source for source, _, _ in work], ("status", "gap", "cursor", "locator")
        )
        continue_turn(turn)


def _literal_citations(turn, work):
    """Keep one shared citation budget across all surfaces of a record."""
    hits = turn.hits.filter(
        case_id=turn.case_id,
        source__case_id=turn.case_id,
        source__turn=turn,
        source__record_key__in={source.record_key for source, _, _ in work},
    )
    counts = dict(hits.values_list("source__record_key").annotate(Count("pk")))
    room = [
        source.pk
        for source, _, _ in work
        if counts.get(source.record_key, 0) < LITERAL_CITATIONS_PER_RECORD
    ]
    seen = set(hits.filter(source_id__in=room).values_list("source_id", "start", "end"))
    return counts, seen


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
        activity.record(turn, "summary", windows)

        def authorize_examples(fragments, **kwargs):
            return authorize_turn(turn, fragments, **kwargs)

        draft, used = engine.representative_answer(
            turn,
            authorize_examples,
            turn.plan["question"],
            windows,
        )
        from .investigation import _supported

        draft = _supported(turn, draft, used)
        authorize_turn(turn, used)
        turn.plan["draft"] = draft
        turn.plan["examples"] = len(used)


def verify_batch(turn):
    """Verify outside the case lock and pin the revisions at publication."""
    activity.record(turn, "verify")
    case = authorize_turn(turn)
    versions = evidence_versions(case)
    accounted = turn.sources.filter(case=case).exclude(status="context")
    candidates = accounted.order_by("pk")
    if turn.plan.get("unreviewed"):
        # An untouched entry has supplied no model input, claim or match.
        # The saved reply still validates its manifest membership in PostgreSQL.
        candidates = candidates.exclude(status="unread", cursor=0)
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
        authorize_turn(turn, lock=True)
        current_versions = evidence_versions(case, lock=True)
        turn.plan["stage"] = "verify"
        turn.plan["verify_versions"] = current_versions
        if current_versions != versions:
            turn.plan.pop("verified_through", None)
            continue_turn(turn)
        elif len(page) == STRUCTURED_BATCH:
            turn.plan["verified_through"] = str(page[-1].pk)
            continue_turn(turn)
        else:
            _finish_review(turn, accounted)


def _finish_review(turn, accounted):
    if (
        turn.reviewed + turn.plan.get("unreviewed", 0) != turn.total
        or accounted.filter(status__in=PENDING_REVIEW).exists()
    ):
        raise AnalysisError("source_changed")
    records = accounted.filter(status="match").values("record_key").distinct().count()
    if turn.plan.get("unreviewed"):
        # A long source can have durable cited hits even when its later excerpts
        # were outside the approved generation budget.
        records = (
            turn.hits.filter(case=turn.case, source__turn=turn, source__case=turn.case)
            .values("source__record_key")
            .distinct()
            .count()
        )
    turn.plan.setdefault("stop_reason", "review_complete")
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
    if turn.plan.get("unreviewed"):
        summary = (
            f"The review reached its model usage limit. Fully reviewed "
            f"{turn.reviewed} of {turn.total} source entries. "
            f"{records} distinct records have cited matches."
        )
        turn.plan["stop_reason"] = "budget"
    if turn.gaps:
        summary += f" Coverage is incomplete for {turn.gaps} source entries."
    lead = "Review the sources with incomplete coverage." if turn.gaps else ""
    draft = turn.plan.pop("draft", None) or engine.empty_answer()
    if n := turn.plan.get("examples"):
        summary += f" The narrative uses {n} representative matching records."
    if draft.get("summary"):
        summary += " " + draft["summary"]
    draft["summary"] = summary
    draft["lead"] = draft.get("lead") or lead
    complete_turn(turn, draft)

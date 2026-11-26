"""Checkpoint case chat reviews and publish only validated, complete answers."""

import logging
import re
from functools import partial
from hashlib import sha256
from itertools import batched
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import Count
from django.http import Http404
from django.utils import timezone

from cases.models import CaseActivity
from core.analysis.errors import AnalysisError
from core.analysis.inference_payloads import encode_json
from core.case_search.indexing import authorize
from core.case_search.parents import current_parent_ids
from core.models import ChatMatch, ChatSource, ChatTurn

from . import activity, ai, corpus, decisions, decisions_approval, engine, engine_counts
from .search_budget import reserve_lookup, timed_query
from .search_scope import question_filters, review_population

logger = logging.getLogger(__name__)

WINDOW = 900
OVERLAP = 120
REVIEW_BATCH = 4
STRUCTURED_BATCH = 64
INITIAL_WINDOWS = 4
ANSWER_WINDOWS = 10
COMPLEX_SEARCH = re.compile(
    r"\b(compare|comparison|contradict\w*|disput\w*|conflict\w*|timeline|chronolog\w*|disagree\w*|sequence|reconcil\w*)\b",
    re.I,
)
MESSAGE_TIMING = re.compile(
    r"\b(?:timing|intervals?|latenc(?:y|ies)|response times?|reply times?|"
    r"(?:message|response|reply) delays?)\b",
    re.I,
)
COMMUNICATION_CONTEXT = re.compile(
    r"\b(?:(?:text|direct) messages?|message timing|texts|texting|chats?|"
    r"conversations?|repl(?:y|ies)|recorded exchanges?)\b",
    re.I,
)
LITERAL_CITATIONS_PER_RECORD = 4
PENDING_REVIEW = ("pending", "pending_hit")
FULL_REVIEW_VERSION = 2


def review_restart_code(turn):
    if turn.error_code == "chat_review_scope_retired":
        return turn.error_code
    version = turn.plan.get("review_version")
    if turn.mode != "full" or version == FULL_REVIEW_VERSION:
        return ""
    if review_population(turn.question) == "text-message":
        if version != 1 or question_filters(turn).get("record_type") != "text-message":
            return "chat_review_scope_retired"
    previous = _preceding_review_clarification(turn)
    if previous and review_population(previous.question) == "text-message":
        if (
            version != 1
            or turn.plan.get("scoped_clarification") != str(previous.pk)
            or question_filters(previous).get("record_type") != "text-message"
            or question_filters(turn).get("record_type", "text-message")
            != "text-message"
        ):
            return "chat_review_scope_retired"
    if version == 1:
        return ""
    if question_filters(turn):
        return "chat_review_scope_retired"
    if turn.plan.get("literal"):
        return "chat_literal_review_retired"
    return ""


def enqueue_turn(turn):
    from .tasks import process_chat_turn

    process_chat_turn.enqueue(str(turn.pk), str(turn.delivery_token))


def _authorize(turn, windows=(), *, lock=False, metadata=False, review=False):
    case = authorize(turn.asked_by_id, turn.case_id, edit=True, lock=lock)
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
    if turn.review_of_id:
        from .deep_review import parent_for

        parent_for(turn)
    if turn.plan.get("approved_proposal"):
        from .proposals import check_continuation

        check_continuation(turn)
    if metadata:
        return case
    if digest := turn.plan.get("history_digest"):
        if sha256(encode_json(_history(turn))).hexdigest() != digest:
            raise AnalysisError("source_changed")
    if versions := turn.plan.get("review_question_sources"):
        # A resolved follow-up can contain evidence from a different review
        # batch. Its sources must still be available whenever that wording is used.
        locators = dict(
            turn.sources.filter(case=case, key__in=versions).values_list(
                "key", "locator"
            )
        )
        read = partial(_read_context_window, turn)
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
    parents = {p for w in windows for p in w.locator.get("search_parents", ())}
    if (
        parents
        and current_parent_ids(case, parents, through=turn.started_at) != parents
    ):
        raise AnalysisError("source_changed")
    for window in windows:
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


def _full_reply_context(previous, turn, hints, read, dependencies):
    """Resolve a small sample in the same order as the exhaustive match list."""
    from .history import recalled_sources_available

    if review_restart_code(previous) or not recalled_sources_available(previous, read):
        return ""
    hits = (
        previous.hits.filter(
            case_id=turn.case_id,
            source__turn=previous,
            source__case_id=turn.case_id,
            source__status="match",
        )
        .select_related("source")
        .order_by("source__record_key", "source__key", "start", "end")[:6]
    )
    excerpts = []
    for position, hit in enumerate(hits, 1):
        ref = corpus.cite(
            turn.case,
            hit.source.locator,
            hit.start,
            hit.quote,
            include_archived=turn.include_archived,
            read=read,
        )
        if ref:
            dependencies.append(hit.source_id)
            if hints is not None:
                hints.append((hit.source.locator, hit.start))
            excerpts.append(
                f"Match {position} · {ref['label'][:100]} · {ref['quote'][:150]}"
            )
    return "\n".join(excerpts)[:600]


def _read_context_window(turn, case, locator, start, size, *, include_archived=False):
    """Keep refreshed context stamps on the active turn's checkpoint."""
    key = f"{locator['key']}@{locator['version']}"
    recorded = locator.get("verified_body")
    checkpoint = turn.plan.get("context_bodies", {}).get(key)
    if checkpoint and (not recorded or checkpoint["revision"] > recorded["revision"]):
        locator = locator | {"verified_body": checkpoint}
    window = corpus.read_window(
        case, locator, start, size, include_archived=include_archived, review=True
    )
    if window and (stamp := window.get("verified_body")) and stamp != recorded:
        turn.plan.setdefault("context_bodies", {})[key] = stamp
    return window


def _ordinary_reply_context(previous, turn, hints, read, *, recalled):
    from .services import inspect_turn

    answer = inspect_turn(previous, read=read)
    if answer is None:
        return None
    topic = answer.get("clarification") or " ".join(
        x["text"] for x in answer.get("claims", [])[:2]
    )
    if recalled and answer.get("lead"):
        topic = f"{topic[:220]} Suggested next check: {answer['lead'][:150]}".strip()
    if topic and any(
        read(turn.case, source.locator, 0, 1) is None
        for source in previous.sources.filter(case=turn.case).iterator()
    ):
        return ""
    if topic and hints is not None:
        refs = [
            ref
            for claim in previous.answer.get("claims", [])[:2]
            for ref in claim["citations"][:2]
        ]
        locators = dict(
            previous.sources.filter(
                case=turn.case, key__in=[r["key"] for r in refs]
            ).values_list("key", "locator")
        )
        hints.extend(
            (locators[r["key"]], r["start"]) for r in refs if r["key"] in locators
        )
    return topic[:400]


def _history(turn, *, hints=None):
    from .history import MAX_DEPENDENCIES, history_turns

    turns = history_turns(turn, select=hints is not None)
    history = {}
    source_ids = set()
    read = partial(_read_context_window, turn)
    recalled_turn = turn.plan.get("recalled_turn")
    # Give explicitly recalled and recent context first use of the source budget.
    for previous in sorted(
        turns, key=lambda row: (str(row.pk) == recalled_turn, row.ordinal), reverse=True
    ):
        recalled = str(previous.pk) == recalled_turn
        # Hold hints until we know all dependencies of the topic fit the budget.
        pointers = (
            [] if hints is not None and (recalled or previous == turns[-1]) else None
        )
        dependencies = []
        if previous.mode == "full":
            topic = _full_reply_context(previous, turn, pointers, read, dependencies)
            if recalled and not topic:
                continue
        else:
            topic = _ordinary_reply_context(
                previous, turn, pointers, read, recalled=recalled
            )
            if topic is None:
                continue
            if topic:
                dependencies.extend(
                    previous.sources.filter(case_id=turn.case_id).values_list(
                        "pk", flat=True
                    )[: MAX_DEPENDENCIES + 1]
                )
        if topic:
            dependencies.extend(previous.plan.get("recalled_sources", ()))

        combined = source_ids.union(str(pk) for pk in dependencies)
        if len(combined) > MAX_DEPENDENCIES:
            continue
        source_ids = combined
        entry = {"question": previous.question[:300], "reply_context": topic}
        if recalled:
            entry["earlier_turn"] = previous.ordinal
        history[previous.ordinal] = entry
        if pointers:
            hints.extend(pointers)
    # Pin the sources of all reused reply context, including inherited sources.
    if source_ids or "recalled_sources" in turn.plan:
        ids = sorted(source_ids)
        if turn.plan.setdefault("recalled_sources", ids) != ids:
            raise AnalysisError("source_changed")
    if history:
        turn.plan["history_sources_pinned"] = True
    return [history[ordinal] for ordinal in sorted(history)]


def _preceding_review_clarification(turn):
    if (
        turn.ordinal <= 1
        or engine.NEW_QUESTION.match(turn.question)
        or turn.question.rstrip().endswith("?")
        or engine.review_requested(turn.question)
    ):
        return None
    previous = (
        ChatTurn.objects.filter(
            case_id=turn.case_id,
            chat_id=turn.chat_id,
            ordinal=turn.ordinal - 1,
            status="complete",
            mode="clarification",
        )
        .only("question", "answer", "plan")
        .first()
    )
    if previous and engine.review_requested(previous.question):
        return previous
    return None


def _review_clarification(turn, history):
    """Allow a reply to continue only the immediately preceding clarification."""
    if not history or not history[-1]["reply_context"]:
        return None
    previous = _preceding_review_clarification(turn)
    if previous and (
        history[-1]["question"] == previous.question[:300]
        and history[-1]["reply_context"]
        == previous.answer.get("clarification", "")[:400]
    ):
        return previous
    return None


def _related_timing_reports(turn, windows, filters):
    from .reports import timing_materials

    if filters or not MESSAGE_TIMING.search(turn.question):
        return []
    messages = [
        window.locator
        for window in windows
        if window.locator["surface"] == "original"
        and window.locator["kind"] == "text-message"
    ]
    if not COMMUNICATION_CONTEXT.search(turn.question) and not (
        messages and re.search(r"\bmessages?\b", turn.question, re.I)
    ):
        return []
    return timing_materials(
        turn.case, through=turn.started_at, sources=messages or None
    )


def _choose_initial_source(turn, windows, filters):
    """Use a bounded preview selection only when it can change the answer context."""
    candidates = windows
    if reports := _related_timing_reports(turn, windows, filters):
        candidates = windows[: decisions.MAX_CANDIDATES - len(reports)] + [
            engine.Window(report.locator, 0, report.text[:WINDOW]) for report in reports
        ]
    if len(candidates) <= INITIAL_WINDOWS:
        return candidates
    previews = [
        {
            "type": f"{w.locator['surface']}/{w.locator['kind']}",
            "label": w.locator["label"],
            "preview": w.text,
        }
        for w in candidates
    ]
    activity.record(turn, "choose_source")
    chosen = decisions.choose_source(
        turn, previews, lambda: _authorize(turn, candidates)
    )
    if chosen is None:
        return None
    # Keep independently ranked evidence beside the chosen lead for comparison.
    first = candidates[chosen]
    return [first, *(w for w in windows if w != first)][:INITIAL_WINDOWS]


def _rank(
    turn,
    query,
    semantic_query,
    hints,
    prior,
    filters,
    *,
    surfaces=None,
    route_sources=False,
    after=None,
):
    from core.case_search.rerank import rerank_order

    from .corpus_search import RECORD_LOOKUP
    from .retrieval_route import related_for_turn

    scope = turn.plan.get("rerank_scope")
    rerank = (
        surfaces is None
        and scope
        and not turn.rerank_started_at
        and (prior or COMPLEX_SEARCH.search(query))
    )
    limit = ANSWER_WINDOWS if turn.plan.get("stage") == "refine" else INITIAL_WINDOWS
    shortlist_limit = decisions.MAX_CANDIDATES if route_sources else limit
    semantic_hints = set() if turn.plan.get("refinement") else None
    with engine.timed(turn, "retrieval"):
        stats = {}
        found = corpus.rank(
            turn.case,
            query,
            user=turn.asked_by,
            include_archived=turn.include_archived,
            limit=24 if rerank else shortlist_limit,
            through=turn.started_at,
            after=after,
            hints=hints,
            filters=filters,
            semantic_hints=semantic_hints,
            stamp_body=True,
            stats=stats,
            message_topics=turn.plan.get("message_topics", ()) if prior else (),
            independent_of=(
                [w.locator for w in prior]
                if turn.plan.get("refinement", {}).get("independent_source")
                else ()
            ),
            surfaces_to_search=surfaces,
            related=lambda _: related_for_turn(
                turn, semantic_query, lambda: _authorize(turn, prior), filters=filters
            ),
        )
        turn.plan["candidate_reads"] = turn.plan.get("candidate_reads", 0) + stats.get(
            "candidates", 0
        )
        if not prior and stats.get("message_topics"):
            turn.plan["message_topics"] = stats["message_topics"]
        if stats.get("forward_copies_skipped"):
            turn.plan["forward_copies_skipped"] = True
        if stats.get("ranked_scope_limited"):
            turn.plan["ranked_scope_limited"] = True
        if stats.get("limited"):
            turn.plan["candidate_limit_reached"] = True
        windows = []
        for locator, start in found:
            row = corpus.read_window(
                turn.case,
                locator,
                start,
                WINDOW,
                include_archived=turn.include_archived,
                review=True,
            )
            if row and row["text"]:
                if "verified_body" in row:
                    locator["verified_body"] = row["verified_body"]
                windows.append(
                    engine.Window(
                        locator,
                        start,
                        row["text"],
                        semantic_match=bool(
                            semantic_hints and (locator["key"], start) in semantic_hints
                        ),
                    )
                )
        if (
            surfaces is None
            and after is None
            and not filters
            and not prior
            and not RECORD_LOOKUP.fullmatch(query.strip())
        ):
            from .analysis_reports import analysis_materials, answer_window

            with timed_query():
                saved = analysis_materials(
                    turn.case, query, through=turn.started_at, automatic=True
                )
            if saved:
                # Keep original passages beside the strongest saved measurement.
                windows.insert(
                    1 if windows else 0, answer_window(saved[0], query, WINDOW)
                )
        if route_sources:
            selected = _choose_initial_source(turn, windows[:shortlist_limit], filters)
            if selected is not None:
                return selected
        if rerank and len(windows) > 1:
            activity.record(turn, "compare", windows)
            order = rerank_order(
                turn.case,
                semantic_query,
                [w.text for w in windows],
                scope=scope,
                authorize=lambda: _authorize(turn, [*prior, *windows]),
                chat_turn=turn,
            )
            windows = [windows[n] for n in order]
    return windows[:limit]


def _row(turn, locator, status):
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


def _store_context(turn, windows):
    ChatSource.objects.bulk_create(
        [_row(turn, w.locator, "context") for w in windows], ignore_conflicts=True
    )


def _complete(turn, result):
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


def _publish_answer(turn, result, windows):
    activity.record(turn, "publish")
    with transaction.atomic():
        case = _authorize(turn, windows, lock=True)
        if "recorded_selection" in turn.plan:
            from . import recorded

            if not recorded.available(turn, lock=True):
                raise AnalysisError("source_changed")
        _store_context(turn, windows)
        known = {w.locator["key"]: w.locator for w in windows}
        from .lineage import corroboration, forwarded_origins

        cited_keys = {
            ref["key"] for claim in result["claims"] for ref in claim["citations"]
        }
        marked = forwarded_origins(case, (known[key] for key in cited_keys))
        support = []
        for claim in result["claims"]:
            cited = {ref["key"] for ref in claim["citations"]}
            support.append(corroboration((known[key] for key in cited), marked))
            for cite in claim["citations"]:
                if not corpus.cite(
                    case,
                    known[cite["key"]],
                    cite["start"],
                    cite["quote"],
                    include_archived=turn.include_archived,
                ):
                    raise AnalysisError("source_changed")
        if support:
            turn.plan["claim_support"] = support
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


def _clear_lookup(turn):
    for key in (
        "refinement",
        "refine_query",
        "refine_windows",
        "search_queries",
        "lookups",
    ):
        turn.plan.pop(key, None)


def _freeze(
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
        case = _authorize(turn, context)
        _store_context(turn, context)
        if turn.review_of_id:
            from .deep_review import manifest_rows

            rows = manifest_rows(turn)
        else:
            rows = (
                _row(turn, locator, "pending")
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
        _authorize(turn, lock=True)
        CaseActivity.objects.select_for_update().get(case=case)
        turn.mode = "full"
        _clear_lookup(turn)
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
        _continue(turn)


def _decide(turn):
    from . import recorded
    from .corpus_search import RECORD_LOOKUP

    activity.record(turn, "question")
    if RECORD_LOOKUP.fullmatch(turn.question.strip()):
        # A named record can be opened and cited directly, even on a follow-up.
        reserve_lookup(turn)
        activity.record(turn, "search")
        windows = _rank(turn, turn.question, turn.question, [], [], {})
        answer = engine.empty_answer() if windows else engine.no_sources_answer()
        for window in windows:
            excerpt = window.text.lstrip()[:240].rstrip()
            if excerpt:
                start = window.start + window.text.index(excerpt)
                answer["claims"].append(
                    {
                        "text": window.locator["label"],
                        "citations": [
                            {
                                "key": window.locator["key"],
                                "start": start,
                                "end": start + len(excerpt),
                                "quote": excerpt,
                            }
                        ],
                    }
                )
        _finish_answer(turn, answer, windows)
        return
    filters, clarification = engine_counts.structured_question(turn, turn.question)
    if clarification:
        _publish_answer(turn, engine.empty_answer(clarification=clarification), [])
        return
    if filters:
        if (
            not turn.question.lstrip()
            .casefold()
            .startswith(("list ", "show ", "find "))
        ):
            activity.record(turn, "query_records")
            answer, windows = recorded.execute(
                turn, recorded.count_selection(turn, filters)
            )
            activity.record(turn, "query_records", windows)
            _finish_answer(turn, answer, windows)
            return
        _freeze(turn, turn.question, filters)
        return
    if literal := engine_counts.literal_question(turn):
        _freeze(turn, turn.question, {}, literal=literal)
        return
    if selection := recorded.direct(turn):
        if selection[0] and (previous := selection[0].get("updates_from")):
            _search_updates(turn, previous)
            return
        activity.record(turn, "query_records")
        if queried := recorded.execute(turn, *selection):
            answer, windows = queried
            activity.record(turn, "query_records", windows)
            if (
                not windows
                and not answer["clarification"]
                and re.search(r"\bsay\b", turn.question, re.I)
                and turn.plan.get("recorded_selection", {})
                .get("membership", {})
                .get("count")
                == 0
            ):
                # Reported speech can live in a transcript or finding even when
                # no retained message body has the wording.
                turn.plan.pop("recorded_selection")
                turn.plan["checked_message_wording"] = True
            else:
                _finish_answer(turn, answer, windows)
                return
    recorded.prepare_context(turn)
    hints = []
    history = _history(turn, hints=hints)
    search = turn.question
    if history:
        turn.plan["history_digest"] = sha256(encode_json(history)).hexdigest()
        context = next(
            (entry for entry in history if "earlier_turn" in entry), history[-1]
        )
        search += " " + context["question"] + " " + context["reply_context"]
    activity.record(turn, "search")
    _answer_search(turn, search, turn.question, history, hints, ())


def _search_updates(turn, previous):
    """Search from the earlier retrieval cutoff, even when older rows rank higher."""
    from .history import updated_materials
    from .search_scope import DATE, TYPE

    base_id = previous.plan.get("update_topic", str(previous.pk))
    base = ChatTurn.objects.filter(
        case=turn.case,
        chat_id=turn.chat_id,
        chat__case=turn.case,
        pk=base_id,
        ordinal__lte=previous.ordinal,
        status="complete",
        mode="ordinary",
    ).first()
    denied = engine.empty_answer(
        clarification=(
            "Which topic and time or evidence scope should I check? "
            "The earlier answer is unavailable for this comparison."
        )
    )
    if not base or not previous.started_at or previous.mode != "ordinary":
        _finish_answer(turn, denied, [])
        return
    turn.plan["history_turns"] = list(dict.fromkeys([str(base.pk), str(previous.pk)]))
    history = _history(turn, hints=[])
    if len(history) != len(turn.plan["history_turns"]):
        _finish_answer(turn, denied, [])
        return
    turn.plan["update_topic"] = str(base.pk)
    turn.plan["history_digest"] = sha256(encode_json(history)).hexdigest()
    reserve_lookup(turn)
    activity.record(turn, "search_updates")
    topic = TYPE.sub("", DATE.sub("", base.question)).strip()
    filters = question_filters(base)
    query = topic + " " + history[0]["reply_context"][:240]
    # Searches against all-time semantic neighbors can lose every new result
    # before their cutoff is applied. Rank only the scoped local indexes here.
    windows = _rank(
        turn,
        query,
        query,
        [],
        [],
        filters,
        surfaces=("original", "attachment", "passage", "finding"),
        after=previous.started_at,
    )
    saved = updated_materials(turn, base, previous.started_at, filters)
    windows = [
        *(engine.Window(item.locator, 0, item.text[:WINDOW]) for item in saved),
        *windows,
    ][:ANSWER_WINDOWS]
    summary = (
        "Searched newly added or restored evidence and newly available analysis "
        f"since the search for answer {previous.ordinal}, using its topic and scope."
    )
    if windows:
        answer, used = engine.representative_answer(
            turn,
            lambda fragments, **kw: _authorize(turn, fragments, **kw),
            "What do these newly available records add to the earlier question? "
            "A newly published analysis may describe observations already known. "
            + topic,
            windows,
            history=history,
        )
        if not answer["clarification"]:
            answer["summary"] = (summary + " " + answer["summary"]).rstrip()
    else:
        answer, used = (
            engine.empty_answer(
                summary + " The bounded search found no relevant new excerpts."
            ),
            [],
        )
    _finish_answer(turn, answer, used)


def _lookup_windows(turn, query, semantic_query, hints, prior, scope):
    from .corpus_navigation import linked_materials
    from .reports import timing_materials
    from .trips import trip_materials

    target = turn.plan.get("refinement", {})
    lookup = target.get("lookup", "refine")
    if lookup == "refine":
        return _rank(turn, query, semantic_query, hints, prior, scope)
    if lookup in ("search_evidence", "search_findings"):
        surfaces = (
            ("original", "passage", "attachment")
            if lookup == "search_evidence"
            else ("finding",)
        )
        return _rank(
            turn, query, semantic_query, hints, prior, scope, surfaces=surfaces
        )
    anchor = None
    if source_key := target.get("source_key"):
        anchor = next(
            (w.locator for w in prior if w.locator["key"] == source_key), None
        )
        if anchor is None:
            raise AnalysisError("source_changed")
    with engine.timed(turn, "retrieval"), timed_query():
        if lookup == "follow_source" and anchor:
            materials = linked_materials(
                turn.case, anchor, query, through=turn.started_at, filters=scope
            )
        elif lookup == "read_trip":
            materials = trip_materials(
                turn.case, query, through=turn.started_at, source=anchor, filters=scope
            )
        elif lookup == "read_analysis":
            from .analysis_reports import analysis_materials, answer_window

            if scope:
                return []
            materials = analysis_materials(
                turn.case,
                query,
                through=turn.started_at,
                sources=[anchor] if anchor else None,
            )
            return [answer_window(material, query, WINDOW) for material in materials]
        elif lookup == "read_timing":
            # A whole saved metric cannot be narrowed to a new date/type filter.
            if scope:
                return []
            materials = timing_materials(
                turn.case, through=turn.started_at, sources=[anchor] if anchor else None
            )
        else:
            raise AnalysisError("model_result_structure")
    return [engine.Window(m.locator, 0, m.text[:WINDOW]) for m in materials]


def _lookup_identity(target):
    lookup = target.get("lookup", "refine")
    query = (
        "" if lookup == "read_timing" else " ".join(target["query"].casefold().split())
    )
    return [lookup, query, target.get("source_key", "")]


def _answer_search(
    turn, query, semantic_query, history, hints, prior, *, refined=False
):
    try:
        reserve_lookup(turn)
        _run_lookup(turn, query, semantic_query, history, hints, prior, refined)
    except AnalysisError as error:
        if error.code != "chat_budget_exhausted" or turn.mode == "full":
            raise
        partial = turn.plan.get("refinement", {}).get("answer") or engine.empty_answer()
        result = partial | {
            "summary": "Processing limit reached. This reply covers the sources read."
        }
        _finish_answer(turn, result, prior)


def _run_lookup(turn, query, semantic_query, history, hints, prior, refined):
    clarification = _review_clarification(turn, history)
    inherited = question_filters(clarification) if clarification else {}
    scope = inherited | question_filters(turn)
    if review_population((clarification or turn).question) == "text-message":
        # Earlier checkpoints could contain other record types, including in
        # source-derived wording carried over from a clarification.
        if (
            refined
            and turn.plan.get("review_version") != FULL_REVIEW_VERSION
            or clarification
            and clarification.plan.get("review_version") != FULL_REVIEW_VERSION
        ):
            raise AnalysisError("chat_review_scope_retired")
        turn.plan["review_version"] = FULL_REVIEW_VERSION
        if scope.get("record_type", "text-message") != "text-message":
            _finish_answer(
                turn,
                engine.empty_answer(
                    clarification=(
                        "The request names messages and a different record type. "
                        "Which type should I review? Please ask again with that type."
                    )
                ),
                [],
            )
            return
        scope["record_type"] = "text-message"
    if inherited:
        # Earlier checkpoints may have included sources outside the clarified scope.
        if refined and turn.plan.get("scoped_clarification") != str(clarification.pk):
            raise AnalysisError("chat_review_scope_retired")
        turn.plan["scoped_clarification"] = str(clarification.pk)
    routing = turn.plan.get("decisions_scope")
    if (
        routing
        and not refined
        and not clarification
        and COMPLEX_SEARCH.search(turn.question)
        and not engine.review_requested(turn.question)
        and decisions_approval.scope(turn.case) == routing
    ):
        windows = _rank(
            turn, query, semantic_query, hints, prior, scope, route_sources=True
        )
    else:
        windows = _lookup_windows(turn, query, semantic_query, hints, prior, scope)
    if refined:
        # Keep the original clues and the new sources available for joint citation.
        missing = [w for w in prior if w not in windows]
        windows = (windows[:6] + missing[:2] + windows[6:] + missing[2:])[
            :ANSWER_WINDOWS
        ]

    def with_prior(fragments):
        return [w for w in prior if w not in fragments] + list(fragments)

    def authorize_context(fragments, **kwargs):
        return _authorize(turn, with_prior(fragments), **kwargs)

    lookups = turn.plan.get("lookups")
    if lookups is None:
        lookups = [
            _lookup_identity({"query": q}) for q in turn.plan.get("search_queries", ())
        ]

    target = turn.plan.get("refinement")
    if refined and target and not _advances(target, windows, prior):
        # Citations in this provisional answer were validated on the last model
        # call. _publish_answer rechecks those originals under the case lock.
        action, result, used = "answer", target["answer"], []
        if target.get("lookup") == "read_trip" and not result["claims"]:
            result = engine.empty_answer(
                "No saved possible trip could be verified for this selection."
            )
        if target.get("lookup") == "read_timing" and not result["claims"]:
            result = engine.empty_answer(
                "The lookup could not verify a saved timing value for this selection."
            )
    elif refined and not windows and not prior:
        action, result, used = "answer", engine.no_sources_answer(), []
    else:
        # The third pass is useful only when the second actually found a
        # new source window. The investigator never pays for repeated queries.
        new_material = any(w not in prior for w in windows)
        remaining = 2 if not refined else int(new_material and len(lookups) < 2)
        activity.record(turn, "answer", windows)
        action, result, used = engine.decide(
            turn,
            authorize_context,
            history,
            windows,
            refined=refined,
            remaining=remaining,
            clarified_review=clarification is not None,
        )
        if action == "refine" and _lookup_identity(result) in [
            _lookup_identity({"query": turn.question}),
            *lookups,
        ]:
            action, result = "answer", result["answer"]
    context = with_prior(used)
    if action == "query_records":
        from . import recorded

        activity.record(turn, "query_records")
        answer, windows = recorded.execute(turn, *result)
        activity.record(turn, "query_records", windows)
        _finish_answer(turn, answer, [*context, *windows])
        return
    if action == "propose_extraction":
        from .proposals import evaluate

        result, context = evaluate(turn, result, context, remaining=remaining)
        action = "answer"
    if action == "refine":
        # Checkpoint the exact context that informed the model's query. A retry
        # must continue this search and recheck its sources before any transfer.
        with transaction.atomic():
            _authorize(turn, context, lock=True)
            _store_context(turn, context)
            turn.plan.update(
                stage="refine",
                refinement=result,
                lookups=[*lookups, _lookup_identity(result)],
                refine_windows=[[w.locator["key"], w.start] for w in context],
            )
            _continue(turn)
        return
    if action in ("review", "review_answer"):
        _freeze(
            turn,
            result,
            {},
            context,
            narrative=action == "review_answer",
            scope=scope,
        )
    else:
        _finish_answer(turn, result, context)


def _finish_answer(turn, result, context):
    if turn.plan.get("candidate_limit_reached"):
        result["summary"] = " ".join(
            filter(
                None,
                (
                    result.get("summary"),
                    "The search reached its candidate limit. Other matches may exist.",
                ),
            )
        )
    _clear_lookup(turn)
    turn.plan["stage"] = "complete"
    _publish_answer(turn, result, context)


def _advances(target, windows, prior):
    """A new search must add material bearing on its outstanding question."""
    from .corpus_search import DISTRIBUTION, terms_for

    query_terms = set(terms_for(target["query"]))
    missing_terms = set(terms_for(target["missing_fact"])) - query_terms
    cited = {
        ref["key"] for claim in target["answer"]["claims"] for ref in claim["citations"]
    }
    known_origins = {
        origin
        for old in prior
        if not cited or old.locator["key"] in cited
        for origin in corpus.original_keys(old.locator)
    }
    known_wording = {
        word
        for old in prior
        if (not cited or old.locator["key"] in cited)
        and (word := old.locator.get("message_lineage", {}).get("wording"))
    }
    previous = sorted(prior, key=lambda w: w.start)
    for window in windows:
        origins = corpus.original_keys(window.locator)
        lineage = window.locator.get("message_lineage", {})
        repeated_forward = (
            lineage.get("forwarded") and lineage.get("wording") in known_wording
        )
        if repeated_forward and not DISTRIBUTION.search(target["missing_fact"]):
            continue
        if target["independent_source"] and (
            lineage.get("forwarded") or lineage.get("wording") in known_wording
        ):
            continue
        if target.get("lookup") in (
            "follow_source",
            "read_analysis",
            "read_timing",
            "read_trip",
        ):
            if (
                window in prior
                or target["independent_source"]
                and (
                    window.locator["surface"] in ("finding", "report", "trip")
                    or known_origins & origins
                )
            ):
                continue
            return True
        same_origin = [
            old for old in previous if origins & corpus.original_keys(old.locator)
        ]
        if (
            same_origin
            and window.locator["surface"] == "finding"
            and target.get("lookup") != "search_findings"
        ):
            continue
        if any(window.text in old.text for old in same_origin):
            continue
        if target["independent_source"] and (
            window.locator["surface"] == "finding" or known_origins & origins
        ):
            continue
        # A shifted window can contain the same search term in its overlap.
        # Test the newly read text, even when several earlier windows cover it.
        start, end = window.start, window.start + len(window.text)
        pos = start
        fresh = []
        for old in same_origin:
            if old.locator["key"] != window.locator["key"]:
                continue
            left, right = max(start, old.start), min(end, old.start + len(old.text))
            if right <= left:
                continue
            if left > pos:
                fresh.append(window.text[pos - start : left - start])
            pos = max(pos, right)
        if pos < end:
            fresh.append(window.text[pos - start :])
        text = " ".join(fresh)
        if not text.strip():
            continue
        whole_window = text == window.text
        # Outline leads and partially repeated windows still need a relevant
        # new passage; only a qualified direct match can bypass the word check.
        if whole_window and window.semantic_match:
            return True
        # The title can identify a newly retrieved original, but mustn't turn a
        # few extra unrelated words from an old original into a new lead.
        if whole_window:
            text += " " + window.locator["label"]
        words = set(re.findall(r"[^\W_]+(?:[-@.][^\W_]+)*", text.casefold()))
        hits = query_terms & words
        if hits and (
            len(hits) >= min(2, len(query_terms))
            or any(re.search(r"\d", word) for word in hits)
            or bool(missing_terms & words)
        ):
            return True
    return False


def _refine(turn):
    target = turn.plan.get("refinement")
    lookup = (target or {}).get("lookup", "refine")
    activity.record(turn, "follow_up" if lookup == "refine" else lookup)
    locators = dict(
        turn.sources.filter(case=turn.case, status="context").values_list(
            "key", "locator"
        )
    )
    prior = []
    hints = []
    for key, start in turn.plan["refine_windows"]:
        locator = locators.get(key)
        row = corpus.read_window(
            turn.case, locator, start, WINDOW, include_archived=turn.include_archived
        )
        if row is None:
            raise AnalysisError("source_changed")
        prior.append(engine.Window(locator, start, row["text"]))
        hints.append((locator, start))
    # Earlier running jobs saved only the query. Let them finish with a
    # model decision since they have no validated answer to publish yet.
    query = target["query"] if target else turn.plan["refine_query"]
    if "lookups" not in turn.plan:
        turn.plan.setdefault("search_queries", [query])
    _answer_search(turn, query, query, _history(turn), hints, prior, refined=True)


def _checked_review(turn, windows, spans):
    try:
        return engine.review(
            turn,
            lambda _, **kw: _authorize(turn, spans, review=True, **kw),
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


def _review_batch(turn):
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
        _verify_batch(turn)
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
    elif literal:
        outcomes = engine_counts.literal_outcomes(literal, readable)
    elif readable:
        outcomes = _checked_review(turn, readable, spans)

    with transaction.atomic():
        # Permission and every excerpt are rechecked under the case lock;
        # stale deliveries cannot advance a cursor or publish any match.
        case = _authorize(turn, spans, lock=True, review=True)
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
        _continue(turn)


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
            return _authorize(turn, fragments, **kwargs)

        draft, used = engine.representative_answer(
            turn,
            authorize_examples,
            turn.plan["question"],
            windows,
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
    activity.record(turn, "verify")
    case = _authorize(turn)
    versions = _versions(case)
    accounted = turn.sources.filter(case=case).exclude(status="context")
    candidates = accounted.order_by("pk")
    if turn.review_of_id and turn.plan.get("unreviewed"):
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
    if turn.review_of_id:
        count = turn.plan["deep_scope"]["count"]
        summary = (
            f"Fully reviewed {turn.reviewed} of {turn.total} verified source entries "
            f"from {count} selected entries. "
            f"{records} distinct records have cited matches."
        )
        if turn.plan.get("approved_limit_reached"):
            summary += " The approved generation limit was reached."
    if turn.gaps:
        summary += f" Coverage is incomplete for {turn.gaps} source entries."
    lead = "Review the sources with incomplete coverage." if turn.gaps else ""
    draft = turn.plan.pop("draft", None) or engine.empty_answer()
    if n := turn.plan.get("examples"):
        summary += f" The narrative uses {n} representative matching records."
    draft["summary"] = summary
    draft["lead"] = draft.get("lead") or lead
    _complete(turn, draft)


def _run_stage(turn):
    try:
        _authorize(turn, review=turn.plan.get("stage") == "review")
        if turn.plan.get("stage") == "prepare_deeper_review" and turn.review_of_id:
            _freeze(turn, turn.question, {}, narrative=True)
        elif turn.plan.get("stage") == "approved_extraction":
            from .proposals import finish_continuation

            finish_continuation(turn)
        elif turn.plan.get("stage") == "verify":
            _verify_batch(turn)
        elif turn.plan.get("stage") == "review":
            if (
                turn.review_of_id
                and turn.review_requests >= turn.plan["deep_scope"]["limit"]
            ):
                raise AnalysisError("chat_deeper_review_limit")
            _review_batch(turn)
        elif turn.plan.get("stage") == "refine":
            _refine(turn)
        else:
            _decide(turn)
    except AnalysisError as error:
        if error.code == "chat_deeper_review_limit" and turn.review_of_id:
            from .deep_review import finish_at_limit

            finish_at_limit(turn)
            return
        if error.code != "chat_budget_exhausted" or turn.mode == "full":
            raise
        # A direct query or linked extraction can hit its limit before producing
        # any verified claims. Discard unverified selection and proposal state.
        turn.plan.pop("recorded_selection", None)
        turn.plan.pop("proposed_extraction", None)
        _finish_answer(
            turn,
            engine.empty_answer(
                "Processing limit reached before this question could be verified."
            ),
            [],
        )


def process_turn(turn_id, token):
    turn = _claim(turn_id, token)
    if turn is None:
        return
    active_token = turn.delivery_token
    try:
        _run_stage(turn)
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


@transaction.atomic
def _fail(turn, active_token, code):
    from cases.models import Case

    from .proposals import resume_idle_chat

    Case.objects.select_for_update().filter(pk=turn.case_id).first()
    updated = ChatTurn.objects.filter(
        pk=turn.pk, delivery_token=active_token, status="running"
    ).update(
        status="failed",
        error_code=code,
        completed_at=timezone.now(),
        activity={
            key: value for key, value in turn.activity.items() if key != "sources"
        },
    )
    if updated:
        resume_idle_chat(turn.case_id, turn.chat_id)

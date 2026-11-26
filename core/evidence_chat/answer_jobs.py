"""Resolve ordinary Evidence Chat answers and checkpoint any follow-up lookup."""

import re
from hashlib import sha256

from django.db import transaction

from core.analysis.errors import AnalysisError
from core.analysis.inference_payloads import encode_json
from core.models import ChatTurn

from . import (
    activity,
    corpus,
    decisions_approval,
    engine,
    engine_counts,
    turn_retrieval,
)
from .checkpoints import (
    authorize_turn,
    clear_lookup,
    complete_turn,
    continue_turn,
    store_context,
)
from .full_review import freeze_review
from .history import reply_context, review_clarification
from .passage_windows import WINDOW
from .search_budget import reserve_lookup
from .search_scope import FULL_REVIEW_VERSION, question_filters, review_population
from .turn_retrieval import ANSWER_WINDOWS, COMPLEX_SEARCH


def start_answer(turn):
    from . import recorded
    from .corpus_search import RECORD_LOOKUP

    activity.record(turn, "question")
    if RECORD_LOOKUP.fullmatch(turn.question.strip()):
        # A named record can be opened and cited directly, even on a follow-up.
        reserve_lookup(turn)
        activity.record(turn, "search")
        windows = turn_retrieval.rank_windows(
            turn, turn.question, turn.question, [], [], {}
        )
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
        finish_answer(turn, answer, windows)
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
            finish_answer(turn, answer, windows)
            return
        freeze_review(turn, turn.question, filters)
        return
    if literal := engine_counts.literal_question(turn):
        freeze_review(turn, turn.question, {}, literal=literal)
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
                finish_answer(turn, answer, windows)
                return
    recorded.prepare_context(turn)
    hints = []
    history = reply_context(turn, hints=hints)
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
        finish_answer(turn, denied, [])
        return
    turn.plan["history_turns"] = list(dict.fromkeys([str(base.pk), str(previous.pk)]))
    history = reply_context(turn, hints=[])
    if len(history) != len(turn.plan["history_turns"]):
        finish_answer(turn, denied, [])
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
    windows = turn_retrieval.rank_windows(
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
            lambda fragments, **kw: authorize_turn(turn, fragments, **kw),
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
    finish_answer(turn, answer, used)


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
        finish_answer(turn, result, prior)


def _run_lookup(turn, query, semantic_query, history, hints, prior, refined):
    clarification = review_clarification(turn, history)
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
            finish_answer(
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
        windows = turn_retrieval.rank_windows(
            turn, query, semantic_query, hints, prior, scope, route_sources=True
        )
    else:
        windows = turn_retrieval.lookup_windows(
            turn, query, semantic_query, hints, prior, scope
        )
    if refined:
        # Keep the original clues and the new sources available for joint citation.
        missing = [w for w in prior if w not in windows]
        windows = (windows[:6] + missing[:2] + windows[6:] + missing[2:])[
            :ANSWER_WINDOWS
        ]

    def with_prior(fragments):
        return [w for w in prior if w not in fragments] + list(fragments)

    def authorize_context(fragments, **kwargs):
        return authorize_turn(turn, with_prior(fragments), **kwargs)

    lookups = turn.plan.get("lookups")
    if lookups is None:
        lookups = [
            _lookup_identity({"query": q}) for q in turn.plan.get("search_queries", ())
        ]

    target = turn.plan.get("refinement")
    if (
        refined
        and target
        and not turn_retrieval.advances_lookup(target, windows, prior)
    ):
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
        finish_answer(turn, answer, [*context, *windows])
        return
    if action == "propose_extraction":
        from .proposals import evaluate

        result, context = evaluate(turn, result, context, remaining=remaining)
        action = "answer"
    if action == "refine":
        # Checkpoint the exact context that informed the model's query. A retry
        # must continue this search and recheck its sources before any transfer.
        with transaction.atomic():
            authorize_turn(turn, context, lock=True)
            store_context(turn, context)
            turn.plan.update(
                stage="refine",
                refinement=result,
                lookups=[*lookups, _lookup_identity(result)],
                refine_windows=[[w.locator["key"], w.start] for w in context],
            )
            continue_turn(turn)
        return
    if action in ("review", "review_answer"):
        freeze_review(
            turn,
            result,
            {},
            context,
            narrative=action == "review_answer",
            scope=scope,
        )
    else:
        finish_answer(turn, result, context)


def refine_answer(turn):
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
    _answer_search(turn, query, query, reply_context(turn), hints, prior, refined=True)


def finish_answer(turn, result, context):
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
    clear_lookup(turn)
    turn.plan["stage"] = "complete"
    _publish_answer(turn, result, context)


def _publish_answer(turn, result, windows):
    activity.record(turn, "publish")
    with transaction.atomic():
        case = authorize_turn(turn, windows, lock=True)
        if "recorded_selection" in turn.plan:
            from . import recorded

            if not recorded.available(turn, lock=True):
                raise AnalysisError("source_changed")
        store_context(turn, windows)
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
        complete_turn(turn, result)

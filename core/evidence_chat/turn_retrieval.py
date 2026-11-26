"""Rank bounded evidence windows and test whether a follow-up adds support."""

import re

from core.analysis.errors import AnalysisError

from . import activity, corpus, decisions, engine
from .checkpoints import authorize_turn
from .passage_windows import WINDOW
from .search_budget import timed_query

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
        turn, previews, lambda: authorize_turn(turn, candidates)
    )
    if chosen is None:
        return None
    # Keep independently ranked evidence beside the chosen lead for comparison.
    first = candidates[chosen]
    return [first, *(w for w in windows if w != first)][:INITIAL_WINDOWS]


def rank_windows(
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
                turn,
                semantic_query,
                lambda: authorize_turn(turn, prior),
                filters=filters,
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
                authorize=lambda: authorize_turn(turn, [*prior, *windows]),
                chat_turn=turn,
            )
            windows = [windows[n] for n in order]
    return windows[:limit]


def lookup_windows(turn, query, semantic_query, hints, prior, scope):
    from .corpus_navigation import linked_materials
    from .reports import timing_materials
    from .trips import trip_materials

    target = turn.plan.get("refinement", {})
    lookup = target.get("lookup", "refine")
    if lookup == "refine":
        return rank_windows(turn, query, semantic_query, hints, prior, scope)
    if lookup in ("search_evidence", "search_findings"):
        surfaces = (
            ("original", "passage", "attachment")
            if lookup == "search_evidence"
            else ("finding",)
        )
        return rank_windows(
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


def advances_lookup(target, windows, prior):
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

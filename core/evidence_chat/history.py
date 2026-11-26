"""Select a bounded, repeatable set of prior turns in this case conversation."""

import re
from datetime import datetime
from functools import partial, reduce
from operator import or_

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F

from core.analysis.errors import AnalysisError
from core.models import ChatSource, ChatTurn
from core.models.chat import chat_topic_vector

from .corpus_search import terms_for
from .search_scope import preceding_review_clarification, review_restart_code

TURN_NUMBER = re.compile(r"\b(?:question|answer|reply|turn)\s*#?\s*(\d{1,8})\b", re.I)
MAX_DEPENDENCIES = 32
CONTEXT_WORDS = frozenset(
    "after again ago answer asked back before chat conversation discussed earlier "
    "first follow following happened last mentioned next previous previously question "
    "recall remember remind reply said same talked then turn yesterday".split()
)


def _earlier_match(rows, question):
    words = terms_for(question, extra_stopwords=CONTEXT_WORDS)
    if not words:
        return None
    query = reduce(or_, (SearchQuery(w, config="simple") for w in words))
    return (
        rows.alias(topics=chat_topic_vector())
        .filter(topics=query)
        .order_by(SearchRank(F("topics"), query).desc(), "-ordinal")
        .first()
    )


def history_turns(turn, *, select):
    rows = ChatTurn.objects.filter(
        case_id=turn.case_id,
        chat_id=turn.chat_id,
        chat__case_id=turn.case_id,
        ordinal__lt=turn.ordinal,
        status="complete",
    ).select_related("case", "chat")
    if "history_turns" in turn.plan:
        # The same conversation entries are revalidated before each transfer and
        # on later deliveries, even if the set of available turns changes.
        return list(rows.filter(pk__in=turn.plan["history_turns"]).order_by("ordinal"))
    recent = list(rows.order_by("-ordinal")[:3])[::-1]
    if select and not turn.plan.get("history_digest"):
        earlier = None
        if number := TURN_NUMBER.search(turn.question):
            earlier = rows.filter(ordinal=int(number[1])).first()
        elif len(recent) == 3 and recent[0].ordinal > 1:
            earlier = _earlier_match(
                rows.filter(ordinal__lt=recent[0].ordinal), turn.question
            )
        if earlier:
            others = [t for t in recent if t.pk != earlier.pk][-2:]
            recent = sorted([earlier, *others], key=lambda t: t.ordinal)
            turn.plan["recalled_turn"] = str(earlier.pk)
        turn.plan["history_turns"] = [str(t.pk) for t in recent]
    return recent


def recalled_sources_available(turn, read):
    if turn.plan.get("history_digest") and not turn.plan.get("history_sources_pinned"):
        # Older replies did not always record the sources used through history.
        return False
    ids = turn.plan.get("recalled_sources", ())
    if not ids:
        return True
    if len(ids) > MAX_DEPENDENCIES:
        return False
    locators = list(
        ChatSource.objects.filter(
            pk__in=ids,
            case_id=turn.case_id,
            turn__case_id=turn.case_id,
            turn__chat_id=turn.chat_id,
            turn__ordinal__lt=turn.ordinal,
            turn__status="complete",
        ).values_list("locator", flat=True)
    )
    return len(locators) == len(ids) and all(
        read(turn.case, locator, 0, 1) is not None for locator in locators
    )


def _full_reply_context(previous, turn, hints, read, dependencies):
    """Resolve a small sample in the same order as the exhaustive match list."""
    from . import corpus

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


def read_context_window(turn, case, locator, start, size, *, include_archived=False):
    """Keep refreshed context stamps on the active turn's checkpoint."""
    from . import corpus

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


def reply_context(turn, *, hints=None):
    turns = history_turns(turn, select=hints is not None)
    history = {}
    source_ids = set()
    read = partial(read_context_window, turn)
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


def review_clarification(turn, history):
    """Allow a reply to continue only the immediately preceding clarification."""
    if not history or not history[-1]["reply_context"]:
        return None
    previous = preceding_review_clarification(turn)
    if previous and (
        history[-1]["question"] == previous.question[:300]
        and history[-1]["reply_context"]
        == previous.answer.get("clarification", "")[:400]
    ):
        return previous
    return None


def updated_materials(turn, previous, after, filters):
    """Find later publications for the reports and possible trips the reply cited."""
    from .analysis_reports import analysis_materials
    from .reports import timing_materials
    from .search_budget import reserve_lookup, timed_query
    from .trips import trip_materials

    keys = {
        ref["key"]
        for claim in previous.answer.get("claims", ())
        for ref in claim["citations"]
    }
    if not 0 < len(keys) <= MAX_DEPENDENCIES:
        return []
    locators = previous.sources.filter(case=turn.case, key__in=keys).values_list(
        "locator", flat=True
    )
    reports, trips = [], []
    for locator in locators:
        if locator["surface"] == "report":
            reports.append(locator)
        elif locator["surface"] == "trip":
            trips.append(locator["pk"])
    materials = []
    if reports and not filters:
        reserve_lookup(turn)
        with timed_query():
            timing = [loc for loc in reports if loc["kind"] == "comm_patterns"]
            if timing:
                materials.extend(
                    timing_materials(
                        turn.case, through=turn.started_at, after=after, sources=timing
                    )
                )
            other = [loc for loc in reports if loc["kind"] != "comm_patterns"]
            if other:
                materials.extend(
                    analysis_materials(
                        turn.case,
                        "",
                        through=turn.started_at,
                        after=after,
                        sources=other,
                        methods={loc["kind"] for loc in other},
                    )
                )
    if trips:
        reserve_lookup(turn)
        with timed_query():
            materials.extend(
                trip_materials(
                    turn.case,
                    "recent",
                    through=turn.started_at,
                    after=after,
                    source=None,
                    filters=filters,
                    prior_trips=trips,
                )
            )
    return materials


def compare_cited_events(turn, previous, order):
    """Compare every cited original with a recorded occurrence or observation clock."""
    from core.evidence.queries import scoped_evidence
    from core.evidence.source_clocks import is_date_only_message
    from core.evidence.types import COMMUNICATION_KINDS, LOCATION_KINDS

    from . import corpus, engine
    from .search_budget import timed_query
    from .services import inspect_turn

    if previous.mode != "ordinary":
        return None
    if inspect_turn(previous) is None:
        return engine.empty_answer(
            "The earlier answer’s cited sources changed and cannot be compared."
        ), []
    keys = {
        ref["key"]
        for claim in previous.answer.get("claims", ())
        for ref in claim["citations"]
    }
    if not 0 < len(keys) <= MAX_DEPENDENCIES:
        return None
    sources = list(previous.sources.filter(case=turn.case, key__in=keys))
    clocks, windows = [], []
    undated = date_only = 0
    selected = {}
    for source in sources:
        loc = source.locator
        sample = corpus.read_window(turn.case, loc, 0, 1)
        if sample is None:
            return None
        windows.append(engine.Window(loc, 0, sample["text"]))
        kind = loc["kind"]
        if loc["surface"] == "trip":
            edge, field = (
                ("First", "start_at") if order == "first" else ("Last", "end_at")
            )
            stamp = loc[field]
            clocks.append(
                (
                    datetime.fromisoformat(stamp),
                    f"{edge} observation (UTC): {stamp}",
                    loc,
                )
            )
        elif loc["surface"] == "original" and kind in (
            *COMMUNICATION_KINDS,
            *LOCATION_KINDS,
        ):
            selected.setdefault(kind, {})[source.evidence_id] = loc

    with timed_query():
        # At most 32 IDs across all types; each owning table resolves one batch.
        for kind, locators in selected.items():
            communication = kind in COMMUNICATION_KINDS
            field = "occurred_at" if communication else "observed_at"
            rows = scoped_evidence(turn.case, kind).filter(
                is_archived=False, pk__in=locators
            )
            if kind == "text-message":
                only_day = is_date_only_message(turn.case)
                date_only += rows.filter(only_day).count()
                rows = rows.exclude(only_day)
            for key, when in rows.values_list("pk", field):
                if when is None:
                    undated += 1
                    continue
                label = (
                    f"Occurred: {when}"
                    if communication
                    else f"Observed at: {when.isoformat()}"
                )
                clocks.append((when, label, locators[key]))
    if not clocks and not selected:
        return None
    result = engine.empty_answer(
        f"Compared recorded occurrence or observation times on {len(clocks)} of "
        f"{len(sources)} sources cited in answer {previous.ordinal}. "
        f"Only precise recorded times can establish the {order} occurrence."
    )
    if date_only:
        result["summary"] += f" {date_only} cited messages give only a calendar date."
    if undated:
        result["summary"] += f" {undated} cited records have no recorded event time."
    if not clocks:
        return result, windows
    extreme = (min if order == "first" else max)(when for when, _, _ in clocks)
    for when, label, loc in clocks:
        if when != extreme:
            continue
        # The generated field follows any free-text original body.
        start = loc.get("metadata_start", 0)
        row = corpus.read_window(turn.case, loc, start, corpus.MAX_WINDOW)
        if not row or (at := ("\n" + row["text"] + "\n").find("\n" + label + "\n")) < 0:
            return None
        window = engine.Window(loc, start + at, label)
        windows.append(window)
        result["claims"].append(
            {
                "text": f"{loc['label']} · {label}",
                "citations": [
                    {
                        "key": loc["key"],
                        "start": window.start,
                        "end": window.start + len(label),
                        "quote": label,
                    }
                ],
            }
        )
    return result, windows

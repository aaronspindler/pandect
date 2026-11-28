"""Select a bounded, repeatable set of prior turns in this case conversation."""

import re
from functools import partial

from core.analysis.errors import AnalysisError
from core.models import ChatSource, ChatTurn

from .search_scope import context_source_allowed, question_filters, review_restart_code

TURN_NUMBER = re.compile(r"\b(?:question|answer|reply|turn)\s*#?\s*(\d{1,8})\b", re.I)
MAX_DEPENDENCIES = 32
CONTEXT_WORDS = frozenset(
    "after again ago answer asked back before chat conversation discussed earlier "
    "first follow following happened last mentioned next previous previously question "
    "recall remember remind reply said same talked then turn yesterday".split()
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

    if not context_source_allowed(case, locator, question_filters(turn)):
        return None
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
    if question_filters(turn):
        # Earlier prose can mix original evidence with facts from outside the
        # new scope. Retrieve current evidence afresh for an explicit filter.
        return []
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

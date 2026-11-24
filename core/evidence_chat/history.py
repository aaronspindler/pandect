"""Select a bounded, repeatable set of prior turns in this case conversation."""

import re
from functools import reduce
from operator import or_

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F

from core.models import ChatSource, ChatTurn
from core.models.chat import chat_topic_vector

from .corpus_search import terms_for

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

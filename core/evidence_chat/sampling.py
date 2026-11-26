"""Choose leads from different parts of a dense local search population."""

import re

from django.contrib.postgres.search import SearchQuery
from django.db.models import Count, F, Q, Window
from django.db.models.functions import MD5, Left, RowNumber

from core.evidence.types import COMMUNICATION_KINDS, LOCATION_KINDS

CHRONOLOGY = re.compile(
    r"\b(when|first|last|earli\w*|latest|before|after|timeline|chronolog\w*|change\w*)\b",
    re.I,
)


def varied_ids(matches, kind, field, top_score, chronology):
    """Sample uncommon accounts and the time boundaries before limiting examples."""
    rows = matches.filter(score__gte=max(1, top_score - 1))
    # This is a sampling group, never evidence of common authorship. Keep the
    # amount of text hashed per matching row bounded, including long documents.
    group = [MD5(Left(field or "title", 512))]
    if kind in COMMUNICATION_KINDS:
        group.append(F("sender_id"))
    candidates = rows.annotate(
        group_size=Window(Count("*"), partition_by=group),
        group_row=Window(RowNumber(), partition_by=group, order_by=["-score", "pk"]),
    ).filter(group_row=1)
    rare = list(
        candidates.order_by("-score", "group_size", "pk").values_list("pk", flat=True)[
            :4
        ]
    )
    if chronology:
        if kind in COMMUNICATION_KINDS:
            clock = "occurred_at"
        elif kind in LOCATION_KINDS:
            clock = "observed_at"
        else:
            return rare
        timed = rows.exclude(**{clock + "__isnull": True})
        edges = (
            timed.order_by(order, "pk").values_list("pk", flat=True).first()
            for order in (clock, "-" + clock)
        )
        return list(dict.fromkeys([*(pk for pk in edges if pk), *rare]))
    return rare


def topic_candidates(rows, case, terms):
    """Find selective indexed words for a large message population before ranking."""

    if len(terms) < 2 or not rows.order_by("pk").values("pk")[4096:4097].exists():
        return rows, ()
    # Ignore common words such as "arrival" when another word identifies the
    # requested subject. Counts are capped so a common term never fetches its
    # entire posting list into Python. The full lexical reader remains available
    # for metadata-only matches, long-text tails, and approved exhaustive work.
    ceiling = 24577
    counts = {}
    for word in terms:
        term = SearchQuery(word, config="simple")
        n = rows.filter(search_words=term).order_by().values("pk")[:ceiling].count()
        if n:
            counts[word] = n
    if not counts or min(counts.values()) == ceiling:
        return rows, ()
    rarest = min(counts.values())
    if max(counts.values()) < ceiling and max(counts.values()) <= rarest * 2:
        return rows, ()
    keys = [word for word, n in counts.items() if n < ceiling and n <= rarest * 2]
    query = topic_query(keys)
    # Sender/recipient names live outside the message text vector. Preserve
    # those leads when restricting the bodies to discriminating topic words.
    from people.models import Person

    named = Q(pk__in=[])
    for word in terms:
        named |= (
            Q(first_name__iexact=word)
            | Q(last_name__iexact=word)
            | Q(email__iexact=word)
            | Q(phone_number__exact=word)
        )
    if Person.objects.filter(named, case=case).exists():
        # A name can match a party even when no message body mentions them.
        # Use the full lexical predicate throughout that conversation.
        return rows, ()
    return rows.filter(search_words=query), tuple(keys)


def topic_query(words):
    query = SearchQuery(words[0], config="simple")
    for word in words[1:]:
        query |= SearchQuery(word, config="simple")
    return query

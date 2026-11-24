"""Rank evidence windows using case record fields and the private passage index."""

import re
from functools import reduce
from itertools import zip_longest
from operator import or_

from django.conf import settings
from django.db.models import Case, Q, TextField, Value, When
from django.db.models.functions import Cast, Lower, StrIndex

from core.case_search.semantic import related_results
from core.evidence.types import COMMUNICATION_KINDS, FILE_KINDS
from text.analysis.synthesis import finding_text

from .corpus import body_field, clipped, material, rows, surfaces

STOPWORDS = frozenset(
    "a an and any are as at about across all be by can case could did do does "
    "evidence every find for from has have how i in into is it me mentions "
    "mention of on or our records regarding say says show tell than that the "
    "their them there these this to was we were what when where which who "
    "with would you know please overview summarize summarise summary overall "
    "key facts give brief available main this far so".split()
)


def terms_for(question):
    words = re.findall(r"[^\W_]+(?:[-@.][^\W_]+)*", question.lower())
    return list(dict.fromkeys(word for word in words if word not in STOPWORDS))[:8]


def fields_for(surface, kind):
    if surface == "passage":
        return ("text",)
    if surface == "finding":
        return ("search_text",)
    if surface == "attachment":
        return ("transcript", "original_filename")
    fields = ["title", "source", "source_url"]
    if kind in FILE_KINDS:
        fields.append("original_filename")
    if kind in ("text", "text-message"):
        fields.append("body")
    if kind in COMMUNICATION_KINDS:
        for party in ("sender", "recipient"):
            fields.extend(
                f"{party}__{field}"
                for field in ("first_name", "last_name", "email", "phone_number")
            )
        fields.append("occurred_at")
    if kind.endswith("location"):
        fields.extend(("address", "latitude", "longitude", "observed_at"))
        if kind == "vehicle-location":
            fields.extend(
                (
                    "vehicle__make",
                    "vehicle__model",
                    "vehicle__license_plate",
                    "vehicle__vin",
                    "raw_row",
                )
            )
        if kind == "person-location":
            fields.extend(
                (
                    "person__first_name",
                    "person__last_name",
                    "person__email",
                    "person__phone_number",
                )
            )
    return fields


def matching_rows(queryset, fields, terms, content, limit):
    conditions = [
        reduce(or_, (Q(**{f"{field}__icontains": term}) for field in fields))
        for term in terms
    ]
    if conditions:
        score = sum(
            Case(When(condition, then=1), default=0) for condition in conditions
        )
        queryset = queryset.filter(reduce(or_, conditions)).annotate(score=score)
    else:
        queryset = queryset.annotate(score=Value(0))
    if content and terms:
        # Each source may match via metadata alone. Keep 0 as that sentinel.
        field = Lower(Cast(content, TextField()))
        offsets = [StrIndex(field, Value(term)) for term in terms]
        queryset = queryset.annotate(
            **{f"match_{i}": offset for i, offset in enumerate(offsets)}
        )
    return queryset.order_by("-score", "pk")[:limit]


def meaning_windows(case, question, user, include_archived, through):
    # Hosted meaning search has its own explicit submission contract. Chat never
    # sends a question or conversation to the hosted embedding service.
    if settings.PANDECT_SEARCH_EMBEDDING_PROVIDER != "local":
        return
    query = " ".join(re.findall(r"\w+", question)[:16])[:200]
    if not query:
        return
    results, _ = related_results(
        case,
        user=user,
        query=query,
        state="all" if include_archived else "active",
        include_source=True,
    )
    for result in results:
        source, pk = result["source"]
        if source == "finding":
            surface, kind = "finding", ""
        elif source == "extraction":
            surface, kind = "passage", ""
        elif source == "attachment":
            surface, kind = "attachment", result["kind"]
        else:
            surface, kind = "original", result["kind"]
        queryset = rows(case, surface, kind, include_archived, through).filter(pk=pk)
        item = clipped(queryset, body_field(surface, kind)).first()
        found = (
            material(case, surface, kind, item, include_archived, through=through)
            if item
            else None
        )
        if found:
            offset = result["start"]
            if surface == "finding":
                offset = max(
                    0, finding_text(item.canonical_item).find(result["excerpt"])
                )
            yield found.locator, max(0, offset - 100)


def ranked_windows(case, question, user, include_archived, limit, through):
    """Score across kinds and retain each distinct surface once."""
    terms = terms_for(question)
    candidates = []
    samples = []
    for surface, kind in surfaces():
        content = body_field(surface, kind)
        type_terms = (
            {word for word in terms if word.removesuffix("s") in kind.split("-")}
            if surface == "original"
            else set()
        )
        keywords = [word for word in terms if word not in type_terms]
        qs = rows(case, surface, kind, include_archived, through)
        qs = clipped(qs, content)
        group = []
        for item in matching_rows(
            qs, fields_for(surface, kind), keywords, content, limit
        ):
            found = material(
                case, surface, kind, item, include_archived, through=through
            )
            if found is None:
                continue
            offsets = [getattr(item, f"match_{i}", 0) for i in range(len(keywords))]
            first = min((offset - 1 for offset in offsets if offset > 0), default=0)
            # A hit only on a recorded participant or date is in the metadata
            # after the original message. Include the evidence for that match.
            if content and keywords and not any(offsets):
                first = getattr(item, "body_length", 0)
            elif surface == "finding":
                body = finding_text(item.canonical_item).lower()
                first = min(
                    (n for word in keywords if (n := body.find(word)) >= 0), default=0
                )
            entry = (item.score + bool(type_terms), found.locator, max(0, first - 100))
            group.append(entry)
        candidates.extend(group)
        if group:
            samples.append(group)
    if terms:
        candidates.sort(
            key=lambda entry: (
                -entry[0],
                entry[1]["surface"] != "original",
                entry[1]["key"],
            )
        )
    else:
        # An overview should show several source types even when one dominates.
        candidates = [
            entry for round_ in zip_longest(*samples) for entry in round_ if entry
        ]
    lexical = [(loc, offset) for _, loc, offset in candidates]
    semantic = list(meaning_windows(case, question, user, include_archived, through))
    # Give the private meaning index room to surface paraphrases in a large case.
    available = {}
    for locator, offset in (
        lexical[: max(1, limit - min(len(semantic), limit // 2))] + semantic + lexical
    ):
        available.setdefault(locator["key"], (locator, offset))
        if len(available) == limit:
            break
    return list(available.values())

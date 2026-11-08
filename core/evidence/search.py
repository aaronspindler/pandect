"""Literal retained-content search and bounded source excerpts.

Call additions are queried through EXISTS so several matching additions never
multiply a call in the paginated evidence collection. Excerpts are fetched only
for the current page, without retrieving complete transcripts or text bodies.
"""

from django.db.models import Exists, OuterRef, Q, Subquery, Value
from django.db.models.functions import Greatest, StrIndex, Substr, Upper

from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS
from phone.models import CallEvidenceAttachment

TEXT_KINDS = ("text", "text-message")
CALL_PARENTS = {"phone-call": "phone_call", "facetime": "facetime"}
EXCERPT_LENGTH = 300


def retained_content_condition(queryset, *, case, kind, query):
    """Return the queryset aliases and predicate to combine with metadata search."""
    if kind in TEXT_KINDS:
        return queryset, Q(body__icontains=query)
    if kind in CALL_KINDS:
        parent = CALL_PARENTS[kind]
        additions = CallEvidenceAttachment.objects.filter(
            Q(original_filename__icontains=query) | Q(transcript__icontains=query),
            case=case,
            **{f"{parent}_id": OuterRef("pk"), f"{parent}__case": case},
        )
        return queryset.alias(matching_call_content=Exists(additions)), Q(
            matching_call_content=True
        )
    return queryset, Q(pk__in=[])


def content_excerpt(field, query):
    # PostgreSQL's UPPER matches the case-insensitive lookup used by Django.
    # Keep extraction in SQL: originals can be up to the captured-text limit.
    start = Greatest(StrIndex(Upper(field), Upper(Value(query))) - 60, Value(1))
    return Substr(field, start, EXCERPT_LENGTH)


def retained_text_matches(*, case, rows, query):
    """Describe one inspectable text match per current-page evidence record.

    Calls link to their first matching retained addition in stable source order;
    all additions remain visible in the original reader. This helper deliberately
    covers retained originals only. Completed derived text can add another source
    to collection search without changing original-content handling.
    """
    if not query:
        return {}
    matches = {}
    for kind in TEXT_KINDS:
        ids = [row["id"] for row in rows if row["kind"] == kind]
        if not ids:
            continue
        items = scoped_evidence(case, kind).filter(pk__in=ids, body__icontains=query)
        for item in items.annotate(excerpt=content_excerpt("body", query)).values(
            "pk", "excerpt"
        ):
            matches[(kind, item["pk"])] = {
                "label": "Original text" if kind == "text" else "Original message",
                "excerpt": item["excerpt"],
            }
    for kind in CALL_KINDS:
        parent = CALL_PARENTS[kind]
        ids = [row["id"] for row in rows if row["kind"] == kind]
        if not ids:
            continue
        additions = (
            CallEvidenceAttachment.objects.filter(
                case=case,
                transcript__icontains=query,
                **{f"{parent}_id": OuterRef("pk"), f"{parent}__case": case},
            )
            .order_by("created_at", "pk")
            .annotate(excerpt=content_excerpt("transcript", query))
        )
        calls = (
            scoped_evidence(case, kind)
            .filter(pk__in=ids)
            .annotate(
                matching_attachment=Subquery(additions.values("pk")[:1]),
                excerpt=Subquery(additions.values("excerpt")[:1]),
            )
        )
        for call in calls.values("pk", "matching_attachment", "excerpt"):
            if call["matching_attachment"]:
                matches[(kind, call["pk"])] = {
                    "label": "First matching supplied transcript",
                    "excerpt": call["excerpt"],
                    "anchor": f"attachment-{call['matching_attachment']}",
                }
    return matches

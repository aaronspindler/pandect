"""Indexed, case-scoped field queries over retained message originals."""

from django.contrib.postgres.aggregates import BitXor
from django.db.models import BigIntegerField, CharField, Count, Func, Q, Value
from django.db.models.functions import Cast, Concat, StrIndex

from core.case_search.documents import FIELDS, searchable
from core.evidence.queries import scoped_evidence
from core.evidence.source_clocks import is_date_only_message
from phone.conversations import pair_messages

GROUP_LIMIT = 12
SELECTION_IDS = 256


def matching(case, selection, *, archived=False):
    pair = selection.get("pair")
    if pair and archived is False:
        # This expression matches the active unordered-participant index.
        rows = pair_messages(case, *pair)
    else:
        rows = scoped_evidence(case, "text-message")
        if pair:
            a, b = pair
            rows = rows.filter(
                Q(sender_id=a, recipient_id=b) | Q(sender_id=b, recipient_id=a)
            )
    rows = rows.filter(
        Q(group_source__isnull=True)
        | Q(communication_context__created_at__lte=selection["through"]),
        created_at__lte=selection["through"],
    )
    excluded = Q(is_archived=True) | Q(group_source__is_archived=True)
    if archived is not None:
        rows = rows.filter(excluded if archived else ~excluded)
    rows = rows.annotate(
        calendar_only=is_date_only_message(case, through=selection["through"])
    )
    for field in ("sender_id", "recipient_id"):
        if selection.get(field):
            rows = rows.filter(**{field: selection[field]})
    if selection.get("after"):
        rows = rows.filter(created_at__gt=selection["after"])
    if selection.get("start") or selection.get("end"):
        clock = Q(calendar_only=False)
        if selection.get("start"):
            clock &= Q(occurred_at__gte=selection["start"])
        if selection.get("end"):
            clock &= Q(occurred_at__lt=selection["end"])
        if selection.get("count_day"):
            # These candidates cannot confirm the requested event day, but the
            # saved coverage statement and its deletion dependencies include them.
            clock |= Q(occurred_at__isnull=True) | Q(calendar_only=True)
        rows = rows.filter(clock)
    phrase = selection["phrase"]
    if not phrase:
        return rows
    # The first predicate matches the existing PostgreSQL trigram index. The
    # second checks the original body, excluding words found only in metadata.
    return searchable(rows, FIELDS["text-message"]).filter(
        search_document__icontains=phrase, body__contains=phrase
    )


def membership(rows):
    # Order-independent and constant space on PostgreSQL, even when thousands
    # of originals share one destination. Recheck on collection revision changes.
    key = Concat(
        Cast("pk", CharField()),
        "sha256",
        Cast("sender_id", CharField()),
        Value("/"),
        Cast("recipient_id", CharField()),
        Value("/"),
        Cast("group_source_id", CharField()),
        Value("/"),
        Cast("occurred_at", CharField()),
        output_field=CharField(),
    )
    signature = Func(
        key, Value(0), function="hashtextextended", output_field=BigIntegerField()
    )
    return rows.aggregate(
        count=Count("pk"),
        undated=Count("pk", filter=Q(occurred_at__isnull=True, calendar_only=False)),
        date_only=Count("pk", filter=Q(calendar_only=True)),
        digest=BitXor(signature),
    )


def representatives(rows, selection):
    operation = selection["field"]
    rows = rows.annotate(phrase_offset=StrIndex("body", Value(selection["phrase"])))
    if operation in ("senders", "recipients"):
        group = (
            ("sender_id",)
            if operation == "senders"
            else ("recipient_id", "group_source_id")
        )
        rows = rows.order_by(*group, "occurred_at", "pk").distinct(*group)
    elif operation in ("first", "last"):
        dated = rows.filter(occurred_at__isnull=False, calendar_only=False)
        clock = "occurred_at" if operation == "first" else "-occurred_at"
        # Show ties rather than implying a unique first or last record.
        rows = dated.filter(
            occurred_at=dated.order_by(clock).values("occurred_at")[:1]
        ).order_by("pk")
    else:
        if selection.get("count_day"):
            rows = rows.filter(calendar_only=False, occurred_at__isnull=False)
        rows = rows.order_by("pk")[:1]
    return list(
        rows.values_list("pk", "phrase_offset", "calendar_only")[: GROUP_LIMIT + 1]
    )


def retention_scope(case, selection, rows, count):
    """Record exact deletion dependencies without retaining every large match set."""
    if count <= SELECTION_IDS:
        return {
            "included": [
                str(pk) for pk in rows.order_by("pk").values_list("pk", flat=True)
            ]
        }
    # Immutable originals, their admission cutoff and these archived exceptions
    # determine which of the matching identities belonged to the initial set.
    exclusions = list(
        matching(case, selection, archived=True)
        .order_by("pk")
        .values_list("pk", flat=True)[: SELECTION_IDS + 1]
    )
    if len(exclusions) <= SELECTION_IDS:
        return {"excluded": [str(pk) for pk in exclusions]}
    return None

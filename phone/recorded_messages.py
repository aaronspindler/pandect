"""Indexed, case-scoped field queries over retained message originals."""

from django.contrib.postgres.aggregates import BitXor
from django.db.models import (
    BigIntegerField,
    Case,
    CharField,
    Count,
    Func,
    Q,
    Value,
    When,
)
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, Coalesce, Concat, StrIndex

from core.case_search.documents import FIELDS, searchable
from core.case_search.sources import name
from core.evidence.queries import scoped_evidence
from core.evidence.source_clocks import is_date_only_message
from phone.conversations import pair_messages

GROUP_LIMIT = 12


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


def source_version():
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
    return Func(
        key, Value(0), function="hashtextextended", output_field=BigIntegerField()
    )


def membership(rows):
    """Revalidate the aggregate signature from previously saved replies."""
    return rows.aggregate(
        count=Count("pk"),
        undated=Count("pk", filter=Q(occurred_at__isnull=True, calendar_only=False)),
        date_only=Count("pk", filter=Q(calendar_only=True)),
        digest=BitXor(source_version()),
    )


def from_snapshot(case, members):
    from django.db.models import Exists, OuterRef

    stored = members.filter(kind="text-message")
    rows = scoped_evidence(case, "text-message").filter(
        is_archived=False, pk__in=stored.values("evidence_id")
    )
    return rows.annotate(
        calendar_only=Exists(stored.filter(evidence_id=OuterRef("pk"), date_only=True))
    )


def _party_keys(rows, operation):
    if operation == "senders":
        return rows, ("sender_id",)
    # The export identifies the provenance. Distinct named sessions or native
    # threads within one export must remain distinguishable.
    chat_name = KeyTextTransform(
        "Chat Session",
        KeyTransform("raw_fields", "communication_context__source_context"),
    )
    rows = rows.annotate(
        session_name=Case(
            When(
                group_source__isnull=False,
                then=Coalesce(chat_name, Value(""), output_field=CharField()),
            ),
            default=Value(""),
            output_field=CharField(),
        ),
        thread_name=Case(
            When(
                group_source__isnull=False,
                then="communication_context__native_thread_id",
            ),
            default=Value(""),
            output_field=CharField(),
        ),
    )
    return rows, ("recipient_id", "group_source_id", "session_name", "thread_name")


def party_totals(case, members, operation):
    rows, keys = _party_keys(from_snapshot(case, members), operation)
    role = "sender" if operation == "senders" else "recipient"
    columns = ["person_name", *keys]
    if role == "recipient":
        columns.append("group_source__title")
    return (
        rows.annotate(person_name=name(role + "__"))
        .values(*columns)
        .annotate(n=Count("pk"))
        .order_by(*columns)
    )


def representatives(rows, selection):
    operation = selection["field"]
    rows = rows.annotate(phrase_offset=StrIndex("body", Value(selection["phrase"])))
    if selection.get("count_day"):
        rows = rows.filter(calendar_only=False, occurred_at__isnull=False)
    if operation in ("senders", "recipients"):
        rows, group = _party_keys(rows, operation)
        rows = rows.order_by(*group, "occurred_at", "pk").distinct(*group)
    elif operation in ("first", "last"):
        dated = rows.filter(occurred_at__isnull=False, calendar_only=False)
        clock = "occurred_at" if operation == "first" else "-occurred_at"
        # Show ties rather than implying a unique first or last record.
        rows = dated.filter(
            occurred_at=dated.order_by(clock).values("occurred_at")[:1]
        ).order_by("pk")
    else:
        rows = rows.order_by("pk")[:1]
    columns = ["pk", "phrase_offset", "calendar_only"]
    if operation == "recipients":
        # PostgreSQL DISTINCT ON needs the named grouping expressions in SELECT.
        columns.extend(("session_name", "thread_name"))
    return [row[:3] for row in rows.values_list(*columns)[: GROUP_LIMIT + 1]]

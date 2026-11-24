"""Day entries for recorded messages and their saved conversation instances."""

from heapq import merge
from itertools import batched

from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.db.models.functions import Substr
from django.http import Http404
from django.utils.text import Truncator

from core.evidence.queries import scoped_evidence
from core.evidence.source_clocks import date_only_messages
from core.models import AnalysisFindingSource
from phone.conversation_instances import (
    MESSAGES_PER_PAGE,
    attach_instance_summaries,
    instance_url,
    instances_touching_messages,
)
from phone.conversations import conversation_url
from phone.models import ConversationSessionMember
from phone.presentation import message_excerpt
from phone.saved_sessions import distinct_sessions, saved_row, valid_sessions


def _message_row(message):
    return {
        "at": message.occurred_at,
        "kind": "message",
        "id": message.pk,
        "sort_id": message.pk,
    }


def _instance_row(instance, members, start, end, batch_ids):
    dated = sorted(
        (m.occurred_at, m.pk)
        for m in members
        if m.occurred_at is not None and start <= m.occurred_at < end
    )
    first_at, first_id = dated[0]
    if first_id not in batch_ids:
        return None
    return _group_row(
        instance,
        first_at,
        first_id,
        dated[-1][0],
        len(dated),
    )


def _group_row(instance, first_at, first_id, last_at, day_count):
    count = instance["count"]
    quantity = f"{count} message{'s' if count != 1 else ''}"
    if day_count < count:
        quantity = f"{day_count} of {quantity} on this day"
    return {
        "at": first_at,
        "end_at": last_at,
        "sort_id": first_id,
        "kind": "conversation",
        "id": instance["id"],
        "detail": f"Inferred session · {quantity}",
        "partial": instance["partial"],
        "instance": instance,
    }


def _saved_day_rows(sessions, start, end):
    dated = ConversationSessionMember.objects.filter(
        session_id=OuterRef("pk"),
        message__occurred_at__gte=start,
        message__occurred_at__lt=end,
    ).order_by("ordinal")
    groups = sessions.annotate(
        first_id=Subquery(dated.values("message_id")[:1])
    ).filter(first_id__isnull=False)
    for session in (
        groups.select_related("grouping")
        .annotate(
            day_start=Subquery(dated.values("message__occurred_at")[:1]),
            day_end=Subquery(
                dated.order_by("-ordinal").values("message__occurred_at")[:1]
            ),
            day_count=Count(
                "members",
                filter=Q(
                    members__message__occurred_at__gte=start,
                    members__message__occurred_at__lt=end,
                ),
            ),
        )
        .order_by("day_start", "first_id", "pk")
        .iterator(chunk_size=50)
    ):
        yield _group_row(
            saved_row(session),
            session.day_start,
            session.first_id,
            session.day_end,
            session.day_count,
        )


def _legacy_day_rows(case, batch, ids, start, end, covered, saved):
    pending = iter(batch)
    message = next(pending)
    for group, members in instances_touching_messages(case, ids, exclude_saved=saved):
        while message and message.occurred_at < group["first_at"]:
            if message.pk not in covered:
                yield _message_row(message)
            message = next(pending, None)
        covered.update(ids.intersection(group["member_ids"]))
        row = _instance_row(group, members, start, end, ids)
        if row:
            yield row
    while message:
        if message.pk not in covered:
            yield _message_row(message)
        message = next(pending, None)


def daily_message_rows(case, start, end):
    """Stream groups once at their first exact original on the selected day."""
    timed = _active_timed_messages(case)
    messages = (
        timed.filter(occurred_at__gte=start, occurred_at__lt=end)
        .only("occurred_at")
        .order_by("occurred_at", "pk")
    )
    # Eligibility is evaluated for the day independently of message pagination.
    # A long session produces one row regardless of how many pages its members span.
    sessions = valid_sessions(case, timed).filter(first_at__lt=end, last_at__gte=start)
    saved_ids = (
        ConversationSessionMember.objects.filter(session__in=sessions)
        .order_by()
        .values("message_id")
    )
    legacy_ids = (
        AnalysisFindingSource.objects.filter(
            case=case,
            kind="text-message",
            finding__result__run__operation="sessions",
            evidence_id__in=messages.order_by().values("pk"),
        )
        .order_by()
        .values("evidence_id")
    )
    standalone = messages.exclude(pk__in=saved_ids).exclude(pk__in=legacy_ids)

    def legacy_rows():
        # A stale legacy finding may leave its original as a standalone entry.
        # A distinct valid legacy finding may overlap an already-saved session.
        candidates = messages.filter(pk__in=legacy_ids).annotate(
            in_saved=Q(pk__in=saved_ids)
        )
        for batch in batched(
            candidates.iterator(chunk_size=MESSAGES_PER_PAGE),
            MESSAGES_PER_PAGE,
            strict=False,
        ):
            ids = {m.pk for m in batch}
            covered = {m.pk for m in batch if m.in_saved}
            yield from _legacy_day_rows(case, batch, ids, start, end, covered, sessions)

    yield from merge(
        (
            _message_row(message)
            for message in standalone.iterator(chunk_size=MESSAGES_PER_PAGE)
        ),
        legacy_rows(),
        _saved_day_rows(distinct_sessions(sessions), start, end),
        key=lambda row: (row["at"], row["sort_id"]),
    )


def _active_timed_messages(case):
    return (
        scoped_evidence(case, "text-message")
        .filter(is_archived=False)
        .exclude(pk__in=date_only_messages(case))
    )


def attach_daily_message_previews(case, rows):
    """Load names, excerpts, exact summaries and overlapping groups for one page."""
    phone_rows = [row for row in rows if row["kind"] in {"message", "conversation"}]
    if not phone_rows:
        return
    originals = (
        scoped_evidence(case, "text-message")
        .filter(is_archived=False)
        .select_related("sender", "recipient")
        .only(
            "sender__first_name",
            "sender__last_name",
            "sender__email",
            "sender__phone_number",
            "recipient__first_name",
            "recipient__last_name",
            "recipient__email",
            "recipient__phone_number",
        )
        .annotate(preview_body=Substr("body", 1, 81))
        .in_bulk({row["sort_id"] for row in phone_rows})
    )
    groups = [row["instance"] for row in phone_rows if row["kind"] == "conversation"]
    attach_instance_summaries(case, groups, preview_only=True)
    published = valid_sessions(case, _active_timed_messages(case))
    for row in phone_rows:
        first = originals.get(row["sort_id"])
        if first is None:
            raise Http404("Message unavailable. Reopen the timeline.")
        row["preview_label"] = str(first.sender)
        row["preview"] = message_excerpt(first.preview_body)
        if row["kind"] == "message":
            row["title"] = f"{first.sender} to {first.recipient}"
            row["url"] = conversation_url(case, first)
            continue
        instance = row.pop("instance")
        row["title"] = f"{first.sender} and {first.recipient}"
        row["url"] = instance_url(
            case, (first.sender_id, first.recipient_id), instance["id"]
        )
        if summary := instance["summary"]:
            row["preview_label"] = (
                "AI summary · Partial coverage"
                if summary["partial"]
                else "AI summary · Whole session"
            )
            row["preview"] = Truncator(summary["preview"]).chars(220)
            row["partial"] |= summary["partial"]
        saved = instance.get("saved_session")
        ids = (
            saved.members.order_by().values("message_id")
            if saved
            else instance["member_ids"]
        )
        shared = ConversationSessionMember.objects.filter(
            session_id=OuterRef("pk"), message_id__in=ids
        )
        others = published.filter(Exists(shared))
        if saved:
            others = others.exclude(membership_digest=saved.membership_digest)
        row["overlap"] = others.exists() or any(
            candidate["id"] != instance["id"]
            for candidate, _ in instances_touching_messages(
                case, ids, exclude_saved=published
            )
        )

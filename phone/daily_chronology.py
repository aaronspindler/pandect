"""Day entries for recorded messages and their saved conversation instances."""

from collections import defaultdict
from itertools import batched

from django.db.models.functions import Substr
from django.http import Http404
from django.utils.text import Truncator

from core.evidence.queries import scoped_evidence
from core.evidence.source_clocks import date_only_messages
from phone.conversation_instances import (
    MESSAGES_PER_PAGE,
    attach_instance_summaries,
    instance_url,
    instances_touching_messages,
)
from phone.conversations import conversation_url
from phone.presentation import message_excerpt


def _message_row(message):
    return {
        "at": message.occurred_at,
        "kind": "message",
        "id": message.pk,
        "sort_id": message.pk,
        "archived": message.is_archived,
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
    count = len(members)
    quantity = f"{count} message{'s' if count != 1 else ''}"
    if len(dated) < count:
        quantity = f"{len(dated)} of {quantity} on this day"
    archived = sum(m.is_archived for m in members)
    detail = f"Inferred session · {quantity}"
    if 0 < archived < count:
        detail += " · Includes archived messages"
    return {
        "at": first_at,
        "end_at": dated[-1][0],
        "sort_id": first_id,
        "kind": "conversation",
        "id": instance["id"],
        "detail": detail,
        "archived": archived == count,
        "partial": instance["partial"],
        "instance": instance,
    }


def daily_message_rows(case, start, end):
    """Stream groups at their first in-day message and uncited originals in order."""
    messages = (
        scoped_evidence(case, "text-message")
        .filter(occurred_at__gte=start, occurred_at__lt=end)
        .exclude(pk__in=date_only_messages(case))
        .only("occurred_at", "is_archived")
        .order_by("occurred_at", "pk")
    )
    for batch in batched(
        messages.iterator(chunk_size=MESSAGES_PER_PAGE), MESSAGES_PER_PAGE, strict=False
    ):
        ids = {message.pk for message in batch}
        covered = set()
        pending = iter(batch)
        message = next(pending)
        # Each group is ordered by its first citation *in this batch*. Earlier
        # originals can now be emitted without a later group hiding them.
        for group, members in instances_touching_messages(case, ids):
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


def attach_daily_message_previews(case, rows):
    """Load names, excerpts, exact summaries and overlapping groups for one page."""
    phone_rows = [row for row in rows if row["kind"] in {"message", "conversation"}]
    if not phone_rows:
        return
    originals = (
        scoped_evidence(case, "text-message")
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
    covered = defaultdict(set)
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
            row["preview_label"] = "AI summary · Whole session"
            row["preview"] = Truncator(summary["preview"]).chars(220)
            row["partial"] |= summary["partial"]
        for member_id in instance["member_ids"]:
            covered[member_id].add(instance["id"])

    # A distinct group may share an off-page or off-day member. Only another
    # current, fully validated instance can establish this overlap.
    overlapping = set()
    if covered:
        for instance, _ in instances_touching_messages(case, covered):
            for member_id in instance["member_ids"]:
                overlapping.update(covered.get(member_id, set()) - {instance["id"]})
    for row in phone_rows:
        row["overlap"] = row["id"] in overlapping

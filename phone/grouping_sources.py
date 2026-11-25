"""Bounded message content and original integrity for session matching."""

from datetime import timedelta
from hashlib import sha256

from django.db import models
from django.db.models import F, OuterRef, Q, Subquery, Value
from django.db.models.functions import (
    Concat,
    Left,
    Length,
    Right,
    Substr,
)

from core.analysis.errors import AnalysisError
from core.storage import canonical_bytes
from phone.conversations import active_pair_messages
from phone.models import CommunicationContext
from phone.session_matching import (
    REPLY_WINDOW,
    WORDING_LIMIT,
    message_context,
    session_decision,
)


def matching_originals(originals):
    # Words influence a limited set of boundaries. A very long original only
    # needs its ends for greeting/farewell checks; never load its entire body.
    return (
        originals.select_related("communication_context__source_document")
        .only(
            "id",
            "case_id",
            "sender_id",
            "recipient_id",
            "group_source_id",
            "occurred_at",
            "sha256",
            "communication_context__case_id",
            "communication_context__message_id",
            "communication_context__source_document__case_id",
            "communication_context__source_document__sha256",
            "communication_context__source_sha256",
            "communication_context__participants",
            "communication_context__native_message_id",
            "communication_context__native_thread_id",
            "communication_context__reply_to_id",
        )
        .annotate(
            body_length=Length("body"),
            matching_text=models.Case(
                models.When(body_length__lte=WORDING_LIMIT, then=F("body")),
                default=Concat(
                    Left("body", WORDING_LIMIT // 2),
                    Value("\n…\n"),
                    Right("body", WORDING_LIMIT // 2),
                    output_field=models.TextField(),
                ),
                output_field=models.TextField(),
            ),
        )
    )


def _original_checksum(row):
    if row.session_wording_complete:
        return sha256(row.original_bytes()).hexdigest()
    # The message checksum covers its entire canonical JSON manifest. Stream
    # just the escaped body field when the original exceeds the wording budget.
    manifest = row.original_data() | {"body": ""}
    before, after = canonical_bytes(manifest).split(b'"body":""', 1)
    digest = sha256(before + b'"body":"')
    chunk_size = 65536
    original = type(row).objects.filter(case_id=row.case_id, pk=row.pk)
    for offset in range(0, row.body_length, chunk_size):
        body_part = original.values_list(
            Substr("body", offset + 1, chunk_size), flat=True
        ).get()
        digest.update(canonical_bytes(body_part)[1:-1])
    digest.update(b'"' + after)
    return digest.hexdigest()


def checked_message(row, case):
    context = message_context(row)
    if context and (
        context.case_id != case.pk
        or context.source_document.case_id != case.pk
        or context.source_document.sha256 != context.source_sha256
    ):
        raise AnalysisError("source_changed")
    row.body = row.matching_text
    row.session_wording_complete = row.body_length <= WORDING_LIMIT
    if _original_checksum(row) != row.sha256:
        raise AnalysisError("source_changed")
    return row


def unique_reply_target(case, current):
    context = message_context(current)
    if not context or not context.reply_to_id:
        return None
    # The identifier belongs to this export. An ambiguous identifier, including
    # a duplicate later in the export, cannot identify which original is cited.
    candidates = list(
        CommunicationContext.objects.filter(
            case=case,
            source_document_id=context.source_document_id,
            native_message_id=context.reply_to_id,
            message__case=case,
        )
        .order_by()
        .values_list("message_id", flat=True)[:2]
    )
    if len(candidates) != 1:
        return None
    return candidates[0]


def delayed_reply_peers(case, batch, anchors, minutes, available_characters):
    """Find delayed, adjacent answers and replies for bounded historical comparisons."""

    messages = active_pair_messages(case).filter(
        created_at__lte=batch.created_at,
        occurred_at__isnull=False,
    )
    same_pair = messages.filter(
        first_person=OuterRef("first_person"),
        second_person=OuterRef("second_person"),
    )
    earlier = same_pair.filter(
        Q(occurred_at__lt=OuterRef("occurred_at"))
        | Q(occurred_at=OuterRef("occurred_at"), pk__lt=OuterRef("pk")),
        occurred_at__gte=OuterRef("occurred_at") - REPLY_WINDOW,
    ).order_by("-occurred_at", "-pk")
    later = same_pair.filter(
        Q(occurred_at__gt=OuterRef("occurred_at"))
        | Q(occurred_at=OuterRef("occurred_at"), pk__gt=OuterRef("pk")),
        occurred_at__lte=OuterRef("occurred_at") + REPLY_WINDOW,
    ).order_by("occurred_at", "pk")
    anchor_ids = {row.pk for row in anchors}
    neighbours = (
        messages.filter(pk__in=anchor_ids)
        .annotate(
            earlier=Subquery(earlier.values("pk")[:1]),
            later=Subquery(later.values("pk")[:1]),
        )
        .order_by()
        .values_list("pk", "earlier", "later")
    )
    pairs = {
        (anchor, peer)
        for anchor, before, after in neighbours
        for peer in (before, after)
        if peer and peer not in anchor_ids
    }
    if not pairs:
        return []
    times = {
        pk: (moment, length)
        for pk, moment, length in messages.filter(
            pk__in={pk for pair in pairs for pk in pair}
        ).values_list("pk", "occurred_at", Length("body"))
    }
    pairs = [
        (anchor, peer)
        for anchor, peer in pairs
        if times[peer][1] <= available_characters
        and abs(times[anchor][0] - times[peer][0]) > timedelta(minutes=minutes)
    ]
    ids = {pk for pair in pairs for pk in pair}
    selected = {
        row.pk: checked_message(row, case)
        for row in matching_originals(messages.filter(pk__in=ids))
    }
    candidates = {}
    for anchor, peer in pairs:
        previous, current = sorted(
            (selected[anchor], selected[peer]), key=lambda row: row.occurred_at
        )
        parent = previous if unique_reply_target(case, current) == previous.pk else None
        decision = session_decision(
            previous, current, gap_minutes=minutes, native_parent=parent
        )
        if decision.continuation:
            neighbour = selected[peer]
            rank = (
                (current.occurred_at - previous.occurred_at),
                neighbour.occurred_at,
                str(peer),
                "text-message",
                neighbour.body_length,
            )
            candidates[peer] = min(rank, candidates.get(peer, rank))
    return candidates.values()

"""Reported-participant conversation queries and evidence navigation."""

from urllib.parse import urlencode

from django.db.models import (
    BooleanField,
    Case,
    CharField,
    Count,
    Exists,
    F,
    Min,
    Q,
    UUIDField,
    Value,
    When,
)
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Collate, Greatest, Least, NullIf, Trim
from django.urls import reverse

from core.evidence.queries import scoped_evidence
from phone.message_csv import WHATSAPP_PARSER_VERSION
from phone.models import CommunicationContext


def active_pair_messages(case):
    """Use the ordered participant expressions from the active pair index."""
    return (
        scoped_evidence(case, "text-message")
        .filter(is_archived=False, recipient_id__isnull=False)
        .alias(
            first_person=Least("sender_id", "recipient_id"),
            second_person=Greatest("sender_id", "recipient_id"),
        )
    )


def pair_messages(case, first_person, second_person):
    first_id, second_id = sorted(
        (
            getattr(first_person, "pk", first_person),
            getattr(second_person, "pk", second_person),
        )
    )
    return active_pair_messages(case).filter(
        first_person=first_id, second_person=second_id
    )


def reported_pair(first_id, second_id):
    """Match either direction of the reported pair on a scoped queryset."""
    return Q(sender_id=first_id, recipient_id=second_id) | Q(
        sender_id=second_id, recipient_id=first_id
    )


def imported_contexts(case):
    return CommunicationContext.objects.filter(
        case=case,
        source_document__case=case,
        message__in=scoped_evidence(case, "text-message"),
    )


def matching_copy_groups(contexts):
    """One native ID, one exact original and at most one row in each export."""
    return (
        contexts.exclude(native_message_id__regex=r"^\s*$")
        .order_by()
        .values("native_message_id")
        .annotate(
            copy_count=Count("pk"),
            export_count=Count("source_document_id", distinct=True),
            original_count=Count("message__sha256", distinct=True),
            dated_count=Count("message__occurred_at"),
            service_count=Count(
                NullIf(Trim(KT("account_identifiers__service")), Value("")),
                distinct=True,
            ),
            whatsapp_count=Count(
                "pk", filter=Q(parser_version=WHATSAPP_PARSER_VERSION)
            ),
            # PostgreSQL has no min(uuid). C ordering matches the UUID tie-break.
            representative=Cast(
                Min(Collate(Cast("message_id", CharField()), "C")), UUIDField()
            ),
        )
        .filter(
            # A blank legacy service cannot make a WhatsApp ID comparable.
            Q(whatsapp_count=0) | Q(whatsapp_count=F("copy_count")),
            copy_count__gt=1,
            export_count=F("copy_count"),
            original_count=1,
            service_count__lte=1,
            dated_count=F("copy_count"),
        )
    )


def timeline_messages(messages, contexts, groups):
    copies = contexts.filter(
        native_message_id__in=groups.values("native_message_id")
    ).values("message_id")
    visible = ~Q(pk__in=copies) | Q(pk__in=groups.values("representative"))
    # Keep this check in the same SQL snapshot as the timeline. With no copy
    # groups, PostgreSQL can skip both membership subqueries entirely.
    include = Case(
        When(Exists(groups), then=visible), default=True, output_field=BooleanField()
    )
    return messages.filter(include).order_by(
        F("occurred_at").asc(nulls_last=True), "pk"
    )


def conversation_list_page(value):
    try:
        return max(1, int(value))
    except TypeError, ValueError:
        return 1


def conversation_url(case, message, *, list_page=1, open_details=True):
    first, second = sorted((message.sender_id, message.recipient_id))
    url = reverse(
        "phone:conversation_detail",
        kwargs={
            "case_id": case.pk,
            "first_person_id": first,
            "second_person_id": second,
        },
    )
    params = {"message" if open_details else "focus": str(message.pk)}
    if list_page > 1:
        params["list_page"] = list_page
    return f"{url}?{urlencode(params)}#message-{message.pk}"


def message_url(case, message):
    if message.recipient_id is None or message.is_archived:
        return reverse(
            "core:evidence:detail", args=(case.pk, "text-message", message.pk)
        )
    return conversation_url(case, message)


def preceding_messages(messages, selected, *, row_id):
    """Use the reader's total order without loading earlier message bodies."""
    if selected.occurred_at is None:
        return messages.filter(
            Q(occurred_at__isnull=False) | Q(occurred_at__isnull=True, pk__lt=row_id)
        )
    return messages.filter(
        Q(occurred_at__lt=selected.occurred_at)
        | Q(occurred_at=selected.occurred_at, pk__lt=row_id)
    )

"""Reported-participant conversation queries and evidence navigation."""

from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse

from core.evidence.queries import scoped_evidence
from phone.models import CommunicationContext


def pair_messages(case, first_person, second_person):
    return scoped_evidence(case, "text-message").filter(
        Q(sender=first_person, recipient=second_person)
        | Q(sender=second_person, recipient=first_person)
    )


def imported_contexts(case):
    return CommunicationContext.objects.filter(
        case=case,
        source_document__case=case,
        message__in=scoped_evidence(case, "text-message"),
    )


def conversation_list_page(value):
    try:
        return max(1, int(value))
    except TypeError, ValueError:
        return 1


def conversation_url(case, message, *, source=None, list_page=1):
    first, second = sorted((message.sender_id, message.recipient_id))
    url = reverse(
        "phone:conversation_detail",
        kwargs={
            "case_id": case.pk,
            "first_person_id": first,
            "second_person_id": second,
        },
    )
    params = {"message": str(message.pk)}
    if source:
        params["source"] = str(source)
    if list_page > 1:
        params["list_page"] = list_page
    return f"{url}?{urlencode(params)}#message-{message.pk}"


def preceding_messages(messages, selected):
    """Use the reader's total order without loading earlier message bodies."""
    if selected.occurred_at is None:
        return messages.filter(
            Q(occurred_at__isnull=False)
            | Q(occurred_at__isnull=True, pk__lt=selected.pk)
        )
    return messages.filter(
        Q(occurred_at__lt=selected.occurred_at)
        | Q(occurred_at=selected.occurred_at, pk__lt=selected.pk)
    )

"""PostgreSQL expressions shared by source queries and their functional indexes."""

import re

from django.contrib.postgres.search import SearchQuery
from django.db.models import F, Q
from django.db.models.functions import Length

from core.evidence.types import FILE_KINDS, KINDS, LOCATION_KINDS

from .expressions import COMMON, VECTOR_CHARS, search_document, word_vector

FIELDS = {
    kind: (
        *COMMON,
        *(("original_filename",) if kind in FILE_KINDS else ()),
        *(("address",) if kind in LOCATION_KINDS else ()),
        *(("body",) if kind in ("text", "text-message") else ()),
    )
    for kind in KINDS
}
FIELDS.update(
    {
        "person": (
            "first_name",
            "last_name",
            "phone_number",
            "email",
            "address",
            "notes",
        ),
        "vehicle": ("make", "model", "license_plate", "vin", "color", "notes"),
        "poi": ("custom_label", "kind", "address", "source", "notes"),
        "finding": ("search_text",),
        "passage": ("text",),
        "attachment": ("original_filename", "transcript", "sha256", "audio_sha256"),
        "account": ("platform", "username", "source"),
        "context": (
            "native_message_id",
            "native_thread_id",
            "reply_to_id",
            "raw_sender",
            "raw_recipient",
        ),
        "trip": ("basis",),
    }
)


def searchable(queryset, fields):
    document = search_document(fields)
    vector = (
        F("search_words")
        if queryset.model is KINDS["text-message"]
        else word_vector(document)
    )
    return queryset.alias(
        search_document=document, search_vector=vector, search_length=Length(document)
    )


def term_condition(term):
    if term.literal:
        return Q(search_document__icontains=term.text)
    return Q(search_vector=SearchQuery(term.text, config="simple")) | Q(
        search_length__gt=VECTOR_CHARS,
        search_document__icontains=term.text,
        search_document__iregex=rf"\m{re.escape(term.text)}\M",
    )


def condition(groups, get_condition=term_condition):
    combined = Q(pk__in=[])
    for group in groups:
        matched = Q()
        for term in group:
            clause = get_condition(term)
            matched &= ~clause if term.negated else clause
        combined |= matched
    return combined

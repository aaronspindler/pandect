"""Summarize verified source relationships separately from similarity of wording."""

import re
from hashlib import sha256

from django.db.models import F, Q

from .corpus import original_keys

FORWARD_MARKER = r"^\s*(?:fwd?:|forwarded(?:\s+message)?\s*:)\s*"
FORWARD = re.compile(FORWARD_MARKER, re.I)
NATIVE_FORWARD = r"^(yes|true|1)$"


def forwarded_messages():
    """The message body or its verified import row explicitly marks a forward."""
    native = "communication_context__source_context__raw_fields__Forwarded"
    return Q(body__iregex=FORWARD_MARKER) | (
        Q(
            communication_context__case_id=F("case_id"),
            communication_context__source_document__case_id=F("case_id"),
            communication_context__source_document__is_archived=False,
            communication_context__source_document__sha256=F(
                "communication_context__source_sha256"
            ),
        )
        & Q(**{native + "__iregex": NATIVE_FORWARD})
    )


def message_lineage(item):
    """Compare full wording. A forward marker supplies no sender identity."""
    prefix = getattr(item, "wording_prefix", "")
    marker = FORWARD.match(prefix)
    forwarded = bool(marker)
    context = getattr(item, "communication_context", None)
    if (
        context
        and context.case_id == item.case_id
        and context.source_document.case_id == item.case_id
        and not context.source_document.is_archived
        and context.source_document.sha256 == context.source_sha256
    ):
        fields = (
            context.source_context.get("raw_fields")
            if isinstance(context.source_context, dict)
            else None
        )
        if isinstance(fields, dict):
            forwarded |= bool(
                re.search(NATIVE_FORWARD, str(fields.get("Forwarded", "")), re.I)
            )
    wording = item.body_digest
    if marker:
        # Longer explicit forwards remain attributable as forwards. Comparing
        # a truncated prefix could wrongly link two different full messages.
        wording = (
            sha256(prefix[marker.end() :].encode()).hexdigest()
            if len(prefix) == item.body_length
            else ""
        )
    return {"wording": wording, "forwarded": forwarded}


def forwarded_origins(case, locators):
    from phone.models import TextMessageEvidence

    keys = {
        key.removeprefix("text-message:")
        for loc in locators
        for key in original_keys(loc)
        if key.startswith("text-message:")
    }
    records = TextMessageEvidence.objects.filter(
        case=case, pk__in=keys, is_archived=False
    )
    return {
        f"text-message:{pk}"
        for pk in records.filter(forwarded_messages()).values_list("pk", flat=True)
    }


def corroboration(locators, marked=()):
    roots = set()
    forwarded, wording = set(), {}
    partial = False
    for loc in locators:
        roots.update(original_keys(loc))
        partial |= loc.get("selected_count", 0) > len(loc.get("dependencies", ()))
        info = loc.get("message_lineage", {})
        key = loc["record_key"]
        if info.get("forwarded"):
            forwarded.add(key)
        if identity := info.get("wording"):
            wording.setdefault(identity, set()).add(key)
    forwarded.update(roots.intersection(marked))
    repeats = sum(len(group) - 1 for group in wording.values())
    return {
        "originals": len(roots),
        "partial": partial,
        "forwards": len(forwarded),
        "repeated_wording": repeats,
    }

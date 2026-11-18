"""Display the exact groups cited by saved, source-validated session findings."""

from itertools import batched

from django.db.models import Exists, F, Func, JSONField, OuterRef, TextField, Value
from django.db.models.functions import Cast
from django.urls import reverse

from core.analysis.catalogue import PER_PAGE, finding_queryset, finding_rows
from core.evidence.types import KIND_LABELS
from core.models import AnalysisFindingSource


def _ordered_members(finding, row):
    """Only a complete, validated set of citations can establish the endpoints."""
    if row["unavailable"] or not isinstance(finding.source_order, list):
        return []
    by_source = {
        source["source_id"]: source["id"]
        for source in row["sources"]
        if source["kind"] == KIND_LABELS["text-message"]
    }
    order = finding.source_order
    if (
        len(by_source) != len(row["sources"])
        or len(order) != len(by_source)
        or not all(isinstance(key, str) for key in order)
        or set(order) != by_source.keys()
    ):
        return []
    return [by_source[key] for key in order]


def attach_session_groups(case, messages):
    visible = {message.pk: message for message in messages}
    for message in messages:
        message.session_groups = []
        message.session_tint = None
        message.session_marker = ""
        message.session_more_url = ""

    citations = AnalysisFindingSource.objects.filter(
        case=case,
        finding_id=OuterRef("pk"),
        kind="text-message",
        evidence_id__in=visible,
    )
    candidates = (
        finding_queryset(case, {"method": "sessions"})
        .filter(Exists(citations))
        .annotate(
            source_order=Func(
                F("result__data"),
                Value("items"),
                Cast(F("item_index"), TextField()),
                Value("source_ids"),
                function="jsonb_extract_path",
                output_field=JSONField(),
            ),
            run_id=F("result__run_id"),
            run_key=F("result__run__equivalence_key"),
            excerpt=F("preview"),
        )
        .only("id", "case_id", "result_id", "item_index", "label", "weak", "preview")
    )
    # Latest first, so equivalent memberships from independent analyses share
    # one presentation and link to the most recent source-validated finding.
    seen = {member: set() for member in visible}
    for batch in batched(candidates.iterator(chunk_size=50), 50, strict=False):
        for finding, row in zip(batch, finding_rows(case, batch), strict=True):
            members = _ordered_members(finding, row)
            membership = frozenset(members)
            visible_members = seen.keys() & membership
            if not visible_members:
                continue
            group = {
                "label": row["label"],
                "reason": row["preview"],
                "url": row["url"],
                "created_at": row["created_at"],
                "partial": row["partial"],
                "count": len(members),
            }
            # Neighbouring sessions within one analysis cycle through the palette.
            tint = 1 + (int(finding.run_key[:8], 16) + finding.item_index) % 6
            for member in visible_members:
                if membership in seen[member]:
                    continue
                message = visible[member]
                if len(seen[member]) == PER_PAGE:
                    message.session_more_url = reverse(
                        "core:analysis:browse",
                        args=(case.pk,),
                        query={
                            "method": "sessions",
                            "kind": "text-message",
                            "evidence": member,
                        },
                    )
                    del seen[member]
                    continue
                seen[member].add(membership)
                message.session_groups.append(group)
                if len(message.session_groups) > 1:
                    message.session_tint = None
                    message.session_marker = "Multiple groupings"
                    continue
                message.session_tint = tint
                if len(members) == 1:
                    message.session_marker = "Single-message session"
                elif member == members[0]:
                    message.session_marker = "Session starts"
                elif member == members[-1]:
                    message.session_marker = "Session ends"
            if not seen:
                return

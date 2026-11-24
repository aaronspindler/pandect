"""Display the exact groups cited by saved, source-validated session findings."""

from itertools import batched

from django.contrib.postgres.expressions import ArraySubquery
from django.db.models import Exists, F, Func, JSONField, OuterRef, TextField, Value
from django.db.models.functions import Cast
from django.urls import reverse

from core.analysis.catalogue import PER_PAGE, finding_queryset, finding_rows
from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS
from core.models import AnalysisFindingSource
from phone.models import ConversationSessionMember
from phone.saved_sessions import (
    distinct_sessions,
    exclude_saved_copies,
    saved_row,
    valid_sessions,
)


def ordered_members(finding, row):
    """Only a complete, validated set of citations can establish the endpoints."""
    if (
        row["unavailable"]
        or not isinstance(finding.source_order, list)
        or any(source["archived"] for source in row["sources"])
    ):
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


def session_findings(case, message_ids):
    citations = AnalysisFindingSource.objects.filter(
        case=case,
        finding_id=OuterRef("pk"),
        kind="text-message",
        evidence_id__in=message_ids,
    )
    return (
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


def attach_session_groups(case, messages):
    visible = {message.pk: message for message in messages}
    for message in messages:
        message.session_groups = []
        message.session_tint = None
        message.session_marker = ""
        message.session_more_url = ""

    candidates = session_findings(case, visible)
    seen = {member: set() for member in visible}

    def attach(member, key, group, tint, marker):
        memberships = seen.get(member)
        if memberships is None or key in memberships:
            return
        message = visible[member]
        if len(memberships) == PER_PAGE:
            if have_saved:
                message.session_more_url = reverse(
                    "phone:conversation_detail",
                    args=(case.pk, *sorted((message.sender_id, message.recipient_id))),
                    query={"tab": "instances", "contains": member},
                )
            else:
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
            return
        memberships.add(key)
        message.session_groups.append(group)
        if len(memberships) > 1:
            message.session_tint = None
            message.session_marker = "Multiple groupings"
        else:
            message.session_tint = tint
            message.session_marker = marker

    saved = valid_sessions(
        case, scoped_evidence(case, "text-message").filter(is_archived=False)
    ).filter(
        Exists(
            ConversationSessionMember.objects.filter(
                session_id=OuterRef("pk"), message_id__in=visible
            )
        )
    )

    have_saved = saved.exists()
    if have_saved:
        members = (
            ConversationSessionMember.objects.filter(
                session__in=distinct_sessions(saved), message_id__in=visible
            )
            .select_related("session__grouping")
            .order_by("-session__grouping__published_at", "-session_id")
        )
        for row in members.iterator(chunk_size=50):
            session = row.session
            group = {
                "label": "Inferred session",
                "reason": row.continuation_reason or session.boundary_reason,
                "url": saved_row(session)["url"],
                "created_at": session.grouping.published_at,
                "partial": False,
                "count": session.message_count,
            }
            marker = _marker(
                session.message_count,
                row.ordinal == 1,
                row.ordinal == session.message_count,
            )
            attach(
                row.message_id,
                session.membership_digest,
                group,
                1 + int(session.membership_digest[:8], 16) % 6,
                marker,
            )
            if not seen:
                return
        refs = AnalysisFindingSource.objects.filter(
            case=case, finding_id=OuterRef("pk"), kind="text-message"
        ).order_by("evidence_id")
        candidates = exclude_saved_copies(
            candidates.alias(member_key=ArraySubquery(refs.values("evidence_id"))),
            saved,
        )
    for batch in batched(candidates.iterator(chunk_size=50), 50, strict=False):
        for finding, row in zip(batch, finding_rows(case, batch), strict=True):
            members = ordered_members(finding, row)
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
                marker = _marker(
                    len(members), member == members[0], member == members[-1]
                )

                attach(member, membership, group, tint, marker)
            if not seen:
                return


def _marker(count, first, last):
    if count == 1:
        return "Single-message session"
    if first:
        return "Session starts"
    return "Session ends" if last else ""

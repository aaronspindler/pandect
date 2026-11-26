"""Follow a bounded number of case evidence links and reported event times."""

import re
from datetime import datetime
from itertools import islice
from uuid import UUID

from django.db.models import F, Value

from core.evidence.daily_chronology import timestamp_fields
from core.evidence.source_clocks import date_only_messages, is_date_only_message
from core.evidence.types import KINDS
from phone.grouping_models import ConversationSessionMember
from phone.grouping_sources import unique_reply_target
from phone.session_matching import message_context, participant_stream

from .corpus import body_field, clipped, material, rows, surfaces
from .passage_windows import WINDOW
from .search_scope import scope_rows

BEFORE = re.compile(r"\b(before|preceded|earlier|previous|prior)\b", re.I)
AFTER = re.compile(r"\b(after|followed|later|next|subsequent)\b", re.I)
AROUND = re.compile(r"\b(timeline|chronology|around|sequence)\b", re.I)


def _replies(case, item, include_archived, through, filters):
    context = message_context(item)
    if not context or (
        context.case_id != case.pk
        or context.source_document.case_id != case.pk
        or context.source_document.sha256 != context.source_sha256
        or (context.source_document.is_archived and not include_archived)
        or through
        and max(context.created_at, context.source_document.created_at) > through
    ):
        return
    messages = (
        scope_rows(
            rows(case, "original", "text-message", include_archived, through),
            case,
            "original",
            "text-message",
            filters,
        )
        .filter(
            communication_context__case=case,
            communication_context__source_document_id=context.source_document_id,
            communication_context__source_sha256=context.source_sha256,
        )
        .defer("body")
    )
    if through:
        messages = messages.filter(communication_context__created_at__lte=through)
    if context.native_thread_id:
        messages = messages.filter(
            communication_context__native_thread_id=context.native_thread_id
        )
    if parent_id := unique_reply_target(case, item):
        parent = messages.filter(pk=parent_id).first()
        if parent and participant_stream(parent) == participant_stream(item):
            yield "original", "text-message", str(parent.pk)
    if context.native_message_id:
        for child in messages.filter(
            communication_context__reply_to_id=context.native_message_id
        ).order_by("occurred_at", "pk")[:2]:
            if (
                participant_stream(child) == participant_stream(item)
                and unique_reply_target(case, child) == item.pk
            ):
                yield "original", "text-message", str(child.pk)


def _closest_event(case, stamp, before, include_archived, through, filters=None):
    # Each evidence table contributes at most one timestamp and source ID.
    order = "-event_at" if before else "event_at"
    lookup = "__lt" if before else "__gt"
    candidates = []
    for kind in KINDS:
        if filters and filters.get("record_type", kind) != kind:
            continue
        for field, _ in timestamp_fields(kind, "observed"):
            qs = scope_rows(
                rows(case, "original", kind, include_archived, through),
                case,
                "original",
                kind,
                filters,
            )
            if kind == "text-message":
                qs = qs.filter(~is_date_only_message(case))
            candidates.append(
                qs.filter(**{field + lookup: stamp})
                .annotate(event_at=F(field), source_kind=Value(kind))
                .values_list("event_at", "source_kind", "pk")
                .order_by(order, "pk")[:1]
            )
    if not candidates:
        return
    nearest = candidates[0]
    if len(candidates) > 1:
        nearest = nearest.union(*candidates[1:], all=True).order_by(
            order, "source_kind", "pk"
        )
    found = nearest.first()
    if found:
        _, kind, pk = found
        yield "original", kind, str(pk)


def nearby_sources(
    case, question, item, loc, include_archived, through, *, filters=None
):
    if loc["surface"] == "trip":
        from .trips import trip_links

        yield from trip_links(case, loc, question, through, filters)
        return
    if loc["surface"] == "report":
        for dep in loc["dependencies"]:
            yield "original", dep["kind"], dep["id"]
        return
    if loc["surface"] == "finding":
        # Use the individual item's cited sources; one summary run can cover a
        # much larger range of messages than a particular finding references.
        cited = set(item.canonical_item.get("source_ids", ()))
        for source in item.result.run.inputs:
            if source["id"] in cited and source["kind"] in KINDS:
                attachment = source.get("attachment_id")
                yield (
                    "attachment" if attachment else "original",
                    source["kind"],
                    str(attachment or source["evidence_id"]),
                )
        return
    if loc["surface"] == "passage":
        siblings = scope_rows(
            rows(case, "passage", "", include_archived, through),
            case,
            "passage",
            "",
            filters,
        ).filter(
            result_id=item.result_id,
            kind=item.kind,
            evidence_id=item.evidence_id,
            evidence_sha256=item.evidence_sha256,
            attachment_id=item.attachment_id,
        )
        for lookup, order in (
            ("item_index__lt", "-item_index"),
            ("item_index__gt", "item_index"),
        ):
            sibling = (
                siblings.filter(**{lookup: item.item_index})
                .order_by(order, "pk")
                .values_list("pk", flat=True)
                .first()
            )
            if sibling:
                yield "passage", "", str(sibling)
        return
    if loc["surface"] != "original":
        return
    if loc["kind"] == "text-message":
        yield from _replies(case, item, include_archived, through, filters)
        if re.search(
            r"\b(conversation|respond\w*|discussion|exchange|disput\w*|repl\w*)\b",
            question,
            re.I,
        ):
            from core.case_search.parents import active_sessions

            sessions = active_sessions(case)
            if through:
                sessions = sessions.filter(grouping__published_at__lte=through)
            members = ConversationSessionMember.objects.filter(
                case=case, session__in=sessions
            )
            current = (
                members.filter(message=item)
                .order_by("-session__grouping__published_at", "pk")
                .first()
            )
            if current:
                adjacent = members.filter(
                    session_id=current.session_id,
                    ordinal__in=[current.ordinal + 1, current.ordinal - 1],
                )
                for pk in adjacent.order_by("ordinal").values_list(
                    "message_id", flat=True
                ):
                    yield "original", "text-message", str(pk)
    before, after = bool(BEFORE.search(question)), bool(AFTER.search(question))
    if AROUND.search(question):
        before = after = True
    if not (before or after):
        return
    if (
        loc["kind"] == "text-message"
        and date_only_messages(case).filter(message_id=item.pk).exists()
    ):
        return
    for field, _ in timestamp_fields(loc["kind"], "observed"):
        stamp = getattr(item, field)
        if stamp:
            if before:
                yield from _closest_event(
                    case, stamp, True, include_archived, through, filters
                )
            if after:
                yield from _closest_event(
                    case, stamp, False, include_archived, through, filters
                )


def linked_materials(case, locator, question, *, through, filters):
    """Open a bounded sample of eligible records linked to a pinned source."""
    surface = locator["surface"]
    kind = locator["kind"] if surface in ("original", "attachment") else ""

    def load(surface, kind, pk, size):
        queryset = scope_rows(
            rows(case, surface, kind, False, through), case, surface, kind, filters
        ).filter(pk=pk)
        item = clipped(
            queryset, body_field(surface, kind), size=size, review=True
        ).first()
        if item is None:
            return None, None
        return item, material(
            case, surface, kind, item, False, size=size, through=through
        )

    try:
        pk = UUID(locator["pk"])
    except ValueError, KeyError, AttributeError, TypeError:
        return []
    if surface == "trip":
        from .trips import read_trip_material

        item = None
        current = read_trip_material(case, pk, 0, 1, through=through)
        if current and (
            filters.get("record_type", "vehicle-location") != "vehicle-location"
            or filters.get("start")
            and datetime.fromisoformat(current.locator["end_at"]) < filters["start"]
            or filters.get("end")
            and datetime.fromisoformat(current.locator["start_at"]) >= filters["end"]
        ):
            return []
    elif surface == "report":
        # Reopen the exact selected report and validate its whole population
        # before any of its source messages can enter the model context.
        if filters:
            return []
        item = None
        from .analysis_reports import read_analysis_material

        current = read_analysis_material(case, pk, 0, 1, through=through)
    elif (surface, kind) in surfaces():
        item, current = load(surface, kind, pk, 1)
    else:
        return []
    if current is None or any(
        locator.get(k) != value
        for k, value in current.locator.items()
        if k not in {"verified_body", "gap"}
    ):
        return []

    links = nearby_sources(
        case, question, item, current.locator, False, through, filters=filters
    )
    found = []
    seen = {(surface, kind, str(pk))}
    for key in islice(links, 16):
        if key in seen:
            continue
        seen.add(key)
        _, target = load(*key, WINDOW)
        if target and target.text:
            found.append(target)
            if len(found) == 4:
                break
    return found

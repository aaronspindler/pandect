"""Freeze and inspect exact case-scoped record populations independently of excerpts."""

from zoneinfo import ZoneInfo

from django.contrib.postgres.functions import RandomUUID
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import BigIntegerField, BooleanField, Count, Func, Q, Value
from django.db.models.functions import Coalesce, TruncWeek
from django.urls import reverse

from core.analysis.errors import AnalysisError
from core.case_search.documents import FIELDS, searchable
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, KIND_LABELS
from core.models import ChatSelectionMember, ChatTurn
from phone import recorded_messages

from . import corpus
from .engine_counts import _clock

# Imported event fields depend on their retained export as well as the event.
IMPORT_DOCUMENT = {
    **dict.fromkeys(CALL_KINDS, "import_row__batch__document"),
    "vehicle-location": "import_batch__document",
    "text-message": "communication_context__source_document",
}


def _populations(case, spec):
    for kind in spec.get("kinds", ["text-message"]):
        if kind == "text-message":
            from .lineage import forwarded_messages

            yield (
                kind,
                recorded_messages.matching(case, spec).annotate(
                    is_forward=Coalesce(forwarded_messages(), Value(False)),
                    version=recorded_messages.source_version(),
                ),
            )
            continue
        rows = corpus.rows(case, "original", kind, False, through=spec["through"])
        if kind == "text" and (phrase := spec.get("phrase")):
            rows = searchable(rows, FIELDS["text"]).filter(
                search_document__icontains=phrase, body__contains=phrase
            )
        if pair := spec.get("pair"):
            a, b = pair
            rows = rows.filter(
                Q(sender_id=a, recipient_id=b) | Q(sender_id=b, recipient_id=a)
            )
        if since := spec.get("after"):
            rows = rows.filter(created_at__gt=since)
        clock = _clock(kind)
        if spec.get("start") or spec.get("end"):
            bounds = Q()
            if spec.get("start"):
                bounds &= Q(**{clock + "__gte": spec["start"]})
            if spec.get("end"):
                bounds &= Q(**{clock + "__lt": spec["end"]})
            if spec.get("count_day"):
                bounds |= Q(**{clock + "__isnull": True})
            rows = rows.filter(bounds)
        yield (
            kind,
            rows.annotate(
                calendar_only=Value(False, output_field=BooleanField()),
                is_forward=Value(False),
                version=Func(
                    "sha256",
                    Value(0),
                    function="hashtextextended",
                    output_field=BigIntegerField(),
                ),
            ),
        )


def populations(turn, spec):
    for kind, rows in _populations(turn.case, spec):
        document = IMPORT_DOCUMENT.get(kind)
        if document:
            rows = rows.exclude(**{document + "__is_archived": True})
        if previous := spec.get("previous_snapshot"):
            since = spec["updated_after"]
            recent = Q(created_at__gt=since) | Q(
                archive_changed_at__gt=since, archive_changed_at__lte=spec["through"]
            )
            if document:
                recent |= Q(
                    **{
                        document + "__archive_changed_at__gt": since,
                        document + "__archive_changed_at__lte": spec["through"],
                    }
                )
            earlier = snapshot_rows(turn, previous).filter(kind=kind)
            rows = rows.filter(recent).exclude(pk__in=earlier.values("evidence_id"))
        yield kind, rows


def snapshot_owner(turn, snapshot):
    rows = ChatTurn.objects.filter(
        pk=snapshot,
        chat_id=turn.chat_id,
        case_id=turn.case_id,
        chat__case_id=turn.case_id,
    )
    if str(snapshot) != str(turn.pk):
        rows = rows.filter(
            ordinal__lt=turn.ordinal,
            status="complete",
            plan__recorded_selection__snapshot=str(snapshot),
        )
    return rows


def snapshot_rows(turn, snapshot):
    # The snapshot belongs to one verified turn. A scalar subquery lets
    # PostgreSQL check that owner once before looking up its members.
    owner_id = snapshot_owner(turn, snapshot).order_by().values("pk")[:1]
    return ChatSelectionMember.objects.filter(turn_id=owner_id)


def snapshot_owner_exists(turn, spec):
    return (
        str(turn.pk) == spec["snapshot"]
        or snapshot_owner(turn, spec["snapshot"]).exists()
    )


def member_ids(turn):
    """Originals confirmed by the recorded predicate. The caller authorizes the case."""
    from . import recorded

    spec = turn.plan.get("recorded_selection", {})
    if "snapshot" not in spec:
        return None
    if turn.status != "complete" or not recorded.available(turn):
        raise AnalysisError("source_changed")
    members = snapshot_rows(turn, spec["snapshot"])
    if spec.get("count_day"):
        members = members.filter(date_only=False, event_at__isnull=False)
    return members.order_by("kind", "evidence_id").values_list("kind", "evidence_id")


@transaction.atomic
def freeze(turn, spec):
    """Copy only identities and recorded clocks, entirely inside PostgreSQL."""
    if (
        not ChatTurn.objects.select_for_update()
        .filter(
            pk=turn.pk,
            case_id=turn.case_id,
            chat__case_id=turn.case_id,
            status="running",
            delivery_token=turn.delivery_token,
        )
        .exists()
    ):
        raise AnalysisError("processing_cancelled")
    members = ChatSelectionMember.objects.filter(turn_id=turn.pk)
    members.delete()  # Redelivery can replace an unpublished checkpoint.
    with connection.cursor() as cursor:
        for kind, rows in populations(turn, spec):
            source = (
                rows.order_by()
                .annotate(
                    member_id=RandomUUID(),
                    owner_id=Value(turn.pk),
                    kind_value=Value(kind),
                )
                .values_list(
                    "member_id",
                    "owner_id",
                    "kind_value",
                    "pk",
                    _clock(kind),
                    "calendar_only",
                    "is_forward",
                    "version",
                )
            )
            sql, params = source.query.sql_with_params()
            cursor.execute(
                "INSERT INTO core_chatselectionmember "
                " (id, turn_id, kind, evidence_id, event_at, date_only, "
                "forwarded, source_version) " + sql,
                params,
            )
    return str(turn.pk)


def totals(rows, *, only_timed=False):
    forwarded = Q(forwarded=True)
    if only_timed:
        forwarded &= Q(event_at__isnull=False, date_only=False)
    return rows.aggregate(
        count=Count("pk"),
        undated=Count("pk", filter=Q(event_at__isnull=True, date_only=False)),
        forwarded=Count("pk", filter=forwarded),
        date_only=Count("pk", filter=Q(date_only=True)),
    )


def current_membership(turn, spec):
    """Compare frozen and current identities, versions, and source clocks in SQL."""
    frozen = snapshot_rows(turn, spec["snapshot"])
    if not snapshot_owner(turn, spec["snapshot"]).exists():
        return False
    for kind, rows in populations(turn, spec):
        actual = rows.order_by().values_list(
            "pk", _clock(kind), "calendar_only", "is_forward", "version"
        )
        prior = (
            frozen.filter(kind=kind)
            .order_by()
            .values_list(
                "evidence_id", "event_at", "date_only", "forwarded", "source_version"
            )
        )
        # Later restoration can admit a record that was outside the historical
        # population. Only the frozen identities and their versions established it.
        if prior.difference(actual).exists():
            return False
    return True


def coverage(spec, members, inspected):
    count = members["count"]
    if spec.get("count_day"):
        count -= members["undated"] + members["date_only"]
    return {
        "matched": count,
        "unknown_time": members["undated"],
        "date_only": members["date_only"],
        "inspected": inspected,
        "forwarded": members["forwarded"],
    }


def group_rows(members, field, timezone_name):
    if field == "types":
        return (
            members.order_by().values("kind").annotate(n=Count("pk")).order_by("kind")
        )
    return (
        members.filter(date_only=False, event_at__isnull=False)
        .annotate(week=TruncWeek("event_at", tzinfo=ZoneInfo(timezone_name)))
        .values("week")
        .annotate(n=Count("pk"))
        .order_by("week")
    )


def group_label(row):
    return KIND_LABELS[row["kind"]] if "kind" in row else row["week"].date().isoformat()


def grouped(members, field, timezone_name):
    return [
        (group_label(row), row["n"])
        for row in group_rows(members, field, timezone_name)[:13]
    ]


def filter_labels(turn):
    from datetime import datetime

    spec = turn.plan["recorded_selection"]
    rows = [
        (
            "Evidence types",
            ", ".join(KIND_LABELS[k] for k in spec.get("kinds", ["text-message"])),
        )
    ]
    for key, label in (
        ("phrase", "Exact wording"),
        ("sender_id", "Recorded sender"),
        ("recipient_id", "Recorded destination"),
    ):
        if value := spec.get(key):
            rows.append((label, value if key == "phrase" else spec["parties"][value]))
    if pair := spec.get("pair"):
        rows.append(
            ("Exchanged between", " and ".join(spec["parties"][key] for key in pair))
        )
    zone = ZoneInfo(turn.plan.get("timezone", "UTC"))
    for key, label in (
        ("start", "Recorded on or after"),
        ("end", "Recorded before"),
        ("through", "Selection cutoff"),
        ("updated_after", "New or restored since"),
        ("after", "Added since"),
    ):
        if value := spec.get(key):
            rows.append(
                (
                    label,
                    datetime.fromisoformat(value)
                    .astimezone(zone)
                    .isoformat(sep=" ", timespec="seconds"),
                )
            )
    return rows


def grouping_choices(spec):
    choices = {"types": "Evidence type", "weeks": "Week"}
    if spec.get("kinds", ["text-message"]) == ["text-message"]:
        choices.update(senders="Recorded sender", recipients="Recorded destination")
    return choices


def _party_row(case_id, row):
    if party := row.get("sender_id") or row.get("recipient_id"):
        label = row["person_name"]
        url = reverse("people:detail", args=(case_id, party))
    else:
        names = [row["session_name"], row["thread_name"]]
        description = (
            " · ".join(dict.fromkeys(n for n in names if n)) or "Name unavailable"
        )
        label = (
            f"Group chat · {description} · {row['group_source__title']} "
            "(individual recipients unknown)"
        )
        url = reverse(
            "core:evidence:detail", args=(case_id, "document", row["group_source_id"])
        )
    return {"label": label, "url": url, "count": row["n"]}


def page(turn, number, group):
    spec = turn.plan.get("recorded_selection", {})
    if not spec.get("snapshot") or turn.status != "complete":
        return None
    members = snapshot_rows(turn, spec["snapshot"]).order_by("kind", "evidence_id")
    if group:
        if spec.get("count_day"):
            members = members.filter(event_at__isnull=False, date_only=False)
        if group in ("senders", "recipients"):
            result = Paginator(
                recorded_messages.party_totals(turn.case, members, group), 50
            ).get_page(number)
            result.object_list = [_party_row(turn.case_id, row) for row in result]
            return result
        result = Paginator(
            group_rows(members, group, turn.plan.get("timezone", "UTC")), 50
        ).get_page(number)
        result.object_list = [
            {"label": group_label(row), "count": row["n"]} for row in result
        ]
        return result
    result = Paginator(members, 50).get_page(number)
    visible = list(result.object_list)
    titles = {}
    for kind in {member.kind for member in visible}:
        ids = [member.evidence_id for member in visible if member.kind == kind]
        titles.update(
            ((kind, pk), title)
            for pk, title in scoped_evidence(turn.case, kind)
            .filter(is_archived=False, pk__in=ids)
            .values_list("pk", "title")
        )
    result.object_list = [
        {
            "kind": KIND_LABELS[item.kind],
            "title": titles[item.kind, item.evidence_id],
            "url": reverse(
                "core:evidence:detail", args=(turn.case_id, item.kind, item.evidence_id)
            ),
            "event_at": item.event_at,
            "date_only": item.date_only,
            "clock": {
                "occurred_at": "Occurrence",
                "observed_at": "Observation",
                "collected_at": "Collection",
            }[_clock(item.kind)],
            "uncertain": spec.get("count_day")
            and (item.event_at is None or item.date_only),
        }
        for item in visible
        if (item.kind, item.evidence_id) in titles
    ]
    return result


def times(rows, field):
    chronological = rows.filter(event_at__isnull=False, date_only=False)
    order = "event_at" if field == "first" else "-event_at"
    return chronological.filter(
        event_at=chronological.order_by(order).values("event_at")[:1]
    )


def answer_for_fields(turn, selection):
    """Group or compare mixed originals at their respective recorded clocks."""
    from . import engine

    members = snapshot_rows(turn, selection["snapshot"])
    if selection.get("count_day"):
        members = members.filter(event_at__isnull=False, date_only=False)
    coverage = selection["coverage"]
    field = selection["field"]
    rows = times(members, field) if field in ("first", "last") else members
    limit = recorded_messages.GROUP_LIMIT if field in ("first", "last") else 1
    if field in ("types", "weeks"):
        totals = grouped(members, field, turn.plan.get("timezone", "UTC"))
    else:
        totals = []
    evidence = list(rows.order_by("kind", "evidence_id")[:limit])
    answer = engine.empty_answer()
    windows = []
    for row in evidence:
        original = corpus.rows(
            turn.case, "original", row.kind, False, through=turn.started_at
        ).filter(pk=row.evidence_id)
        item = corpus.clipped(original, corpus.body_field("original", row.kind)).first()
        if item is None:
            raise AnalysisError("source_changed")
        found = corpus.material(turn.case, "original", row.kind, item, False)
        if not found:
            raise AnalysisError("source_changed")
        ranges = []
        metadata, _, _ = corpus.original_metadata(item, row.kind, value_ranges=ranges)
        offset = 0
        quote = f"Type: {KIND_LABELS[row.kind]}"
        if row.event_at and not row.date_only:
            clock = _clock(row.kind)
            label = {
                "observed_at": "Observed at",
                "occurred_at": "Occurred",
                "collected_at": "Collected",
            }[clock] + ": "
            value = (
                row.event_at.isoformat()
                if clock == "observed_at"
                else str(row.event_at)
            )
            position = next(
                (
                    a
                    for a, b in ranges
                    if metadata[a:b] == value and metadata[:a].endswith(label)
                ),
                None,
            )
            if position is None:
                raise AnalysisError("source_changed")
            quote = label + value
            offset = position - len(label)
        if not metadata.startswith(quote, offset):
            raise AnalysisError("source_changed")
        start = found.locator["metadata_start"] + offset
        windows.append(engine.Window(found.locator, start, quote))
        answer["claims"].append(
            {
                "text": found.locator["label"] + " · " + quote,
                "citations": [
                    {
                        "key": found.locator["key"],
                        "start": start,
                        "end": start + len(quote),
                        "quote": quote,
                    }
                ],
            }
        )
    coverage["inspected"] = len(windows)
    count = coverage["matched"]
    label = ", ".join(KIND_LABELS[k] for k in selection.get("kinds", ["text-message"]))
    unit = "record" if count == 1 else "records"
    summary = f"{count} active {unit} matched the recorded filters ({label})."
    if phrase := selection.get("phrase"):
        summary += f" Exact wording in the original text: “{phrase}”."
    if isinstance(day := selection.get("count_day"), str):
        summary += f" The selected local date is {day}."
    if field in ("types", "weeks"):
        title = (
            "By evidence type"
            if field == "types"
            else "By week beginning in the selected time zone"
        )
        values = [
            f"{label}: {n}" for label, n in totals[: recorded_messages.GROUP_LIMIT]
        ]
        if values:
            summary += " " + title + ": " + "; ".join(values) + "."
        if len(totals) > recorded_messages.GROUP_LIMIT:
            summary += " Showing the first twelve groups."
    elif field in ("first", "last"):
        summary += f" Showing the {field} precisely timed records, including ties."
    if selection.get("after") or selection.get("updated_after"):
        summary += " Showing new or restored matches since that answer."
    answer["summary"] = summary
    return answer, windows

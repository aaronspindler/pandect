"""Complete call summaries with bounded database reads and paginated sources."""

from collections import Counter
from datetime import UTC
from heapq import merge
from itertools import groupby

from django.db.models import Count, Q
from django.db.models.functions import Greatest, Least

from core.analysis.reporting import (
    BATCH_SIZE,
    _base_metrics,
    _group_page,
    _instant,
    _median,
    _period,
    _record_page,
    _source,
)
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, KIND_LABELS
from people.models import Person


def _pairs(queries):
    streams = [
        queryset.order_by()
        .annotate(
            first=Least("sender_id", "recipient_id"),
            second=Greatest("sender_id", "recipient_id"),
        )
        .values("first", "second")
        .annotate(total=Count("pk"))
        .order_by("first", "second")
        .values_list("first", "second", "total")
        .iterator(chunk_size=BATCH_SIZE)
        for _, queryset in queries
    ]
    for pair, rows in groupby(
        merge(*streams, key=lambda row: row[:2]), key=lambda row: row[:2]
    ):
        yield {"ids": tuple(dict.fromkeys(pair)), "count": sum(row[2] for row in rows)}


def call_report(
    case,
    participant_id,
    other_participant_id,
    start,
    end,
    page,
    page_size,
    pair_page,
):
    queries = []
    missing_times = 0
    for kind in CALL_KINDS:
        queryset = (
            scoped_evidence(case, kind)
            .filter(is_archived=False)
            .select_related("sender", "recipient")
        )
        if other_participant_id:
            queryset = queryset.filter(
                Q(sender_id=participant_id, recipient_id=other_participant_id)
                | Q(sender_id=other_participant_id, recipient_id=participant_id)
            )
        elif participant_id:
            queryset = queryset.filter(
                Q(sender_id=participant_id) | Q(recipient_id=participant_id)
            )
        missing_times += queryset.filter(occurred_at__isnull=True).count()
        queries.append((kind, _period(queryset, "occurred_at", start, end)))
    page_obj, rows = _record_page(queries, page, page_size, time_field="occurred_at")
    total = page_obj.paginator.count
    hours = Counter()
    durations = Counter()
    counts = Counter()
    known_count = unknown_times = 0
    duration_total = 0
    minimum = maximum = None
    for kind, queryset in queries:
        for duration, occurred_at in (
            queryset.order_by()
            .values_list("duration", "occurred_at")
            .iterator(chunk_size=BATCH_SIZE)
        ):
            counts[kind] += 1
            if occurred_at is None:
                unknown_times += 1
            else:
                hours[occurred_at.astimezone(UTC).hour] += 1
            seconds = duration.total_seconds() if duration is not None else None
            if seconds is not None:
                known_count += 1
                duration_total += seconds
                minimum = seconds if minimum is None else min(minimum, seconds)
                maximum = seconds if maximum is None else max(maximum, seconds)
            bucket = (
                "Unknown"
                if seconds is None
                else "Zero"
                if seconds == 0
                else "Under 1 minute"
                if seconds < 60
                else "1 to under 5 minutes"
                if seconds < 300
                else "5 minutes or more"
            )
            durations[bucket] += 1
    pairs = _group_page(lambda: _pairs(queries), pair_page)
    people = {
        person.pk: str(person)
        for person in Person.objects.filter(
            case=case, pk__in={pk for pair in pairs for pk in pair["ids"]}
        )
    }
    for pair in pairs:
        pair["label"] = " ↔ ".join(people[pk] for pk in pair["ids"])
    items = []
    for kind, item in rows:
        pair = sorted(
            {
                (str(item.sender_id), str(item.sender)),
                (str(item.recipient_id), str(item.recipient)),
            }
        )
        items.append(
            _source(
                case,
                kind,
                item,
                {
                    "Participant pair": " ↔ ".join(name for _, name in pair),
                    "Sender ID": str(item.sender_id),
                    "Recipient ID": str(item.recipient_id),
                    "Recorded call time (UTC)": _instant(item.occurred_at),
                    "Reported duration (seconds)": item.duration.total_seconds()
                    if item.duration is not None
                    else "Unknown",
                },
                kind_label=KIND_LABELS[kind],
            )
        )
    median_duration = _median(
        [queryset for _, queryset in queries],
        "duration",
        value=lambda duration: duration.total_seconds(),
    )
    metrics = _base_metrics(total, start, end, "Recorded call time")
    metrics.update(
        {
            "Participant filter": str(participant_id)
            if participant_id
            else "All participants",
            "Other participant filter": str(other_participant_id)
            if other_participant_id
            else "Not constrained",
            "Phone calls": counts["phone-call"],
            "FaceTime calls": counts["facetime"],
            "Unknown duration": total - known_count,
            "Zero duration": durations["Zero"],
            "Reported duration total (seconds, known only)": duration_total
            if known_count
            else "Unknown",
            "Reported duration median (seconds, known only)": (
                median_duration if median_duration is not None else "Unknown"
            ),
            "Reported duration minimum (seconds, known only)": minimum
            if minimum is not None
            else "Unknown",
            "Reported duration maximum (seconds, known only)": maximum
            if maximum is not None
            else "Unknown",
            "Duration distribution": "; ".join(
                f"{key}: {value}" for key, value in sorted(durations.items())
            )
            or "No calls",
            "Unknown event time in selection": unknown_times,
            "Missing event time before period filter": missing_times,
            "Recorded UTC hour counts": "; ".join(
                f"{hour:02d}:00–{hour:02d}:59: {count}"
                for hour, count in sorted(hours.items())
            )
            or "No known call times",
        }
    )
    warnings = []
    if (start is not None or end is not None) and missing_times:
        warnings.append(
            "Calls without an event time are excluded by the selected period."
        )
    return {
        "summary": (
            f"{total} selected call records, with {known_count} known "
            "reported durations. Recorded hour counts use UTC."
        ),
        "metrics": metrics,
        "items": items,
        "page_obj": page_obj,
        "pair_page": pairs,
        "warnings": warnings,
        "limitations": [
            "Counts describe retained records, not network-verified activity. Unknown "
            "duration differs from zero; these records do not report answered/missed "
            "status or verify who participated or appeared on camera."
        ],
    }

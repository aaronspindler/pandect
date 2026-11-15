"""Deterministic summaries of retained call records and reported durations."""

from collections import Counter
from datetime import UTC
from statistics import median

from django.db.models import Q

from core.analysis.reporting import (
    _base_metrics,
    _instant,
    _period,
    _selection,
    _source,
    _state,
)
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, KIND_LABELS


def call_report(case, state, participant_id, other_participant_id, start, end, limit):
    queries = []
    missing_times = 0
    for kind in CALL_KINDS:
        queryset = _state(scoped_evidence(case, kind), state).select_related(
            "sender", "recipient"
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
    rows, available, warnings = _selection(queries, limit)
    known = [
        item.duration.total_seconds() for _, item in rows if item.duration is not None
    ]
    hours = Counter()
    pairs = Counter()
    pair_labels = {}
    durations = Counter()
    items = []
    for kind, item in rows:
        pair = sorted(
            {
                (str(item.sender_id), str(item.sender)),
                (str(item.recipient_id), str(item.recipient)),
            }
        )
        pair_label = " ↔ ".join(name for _, name in pair)
        pair_key = tuple(identifier for identifier, _ in pair)
        pairs[pair_key] += 1
        pair_labels[pair_key] = pair_label
        if item.occurred_at is not None:
            hours[item.occurred_at.astimezone(UTC).hour] += 1
        seconds = item.duration.total_seconds() if item.duration is not None else None
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
        items.append(
            _source(
                case,
                kind,
                item,
                {
                    "Participant pair": pair_label,
                    "Sender ID": str(item.sender_id),
                    "Recipient ID": str(item.recipient_id),
                    "Recorded call time (UTC)": _instant(item.occurred_at),
                    "Reported duration (seconds)": seconds
                    if seconds is not None
                    else "Unknown",
                },
                kind_label=KIND_LABELS[kind],
            )
        )
    metrics = _base_metrics(rows, available, state, start, end, "Recorded call time")
    metrics.update(
        {
            "Participant filter": str(participant_id)
            if participant_id
            else "All participants",
            "Other participant filter": str(other_participant_id)
            if other_participant_id
            else "Not constrained",
            "Phone calls": sum(kind == "phone-call" for kind, _ in rows),
            "FaceTime calls": sum(kind == "facetime" for kind, _ in rows),
            "Unknown duration": len(rows) - len(known),
            "Zero duration": sum(value == 0 for value in known),
            "Reported duration total (seconds, known only)": sum(known)
            if known
            else "Unknown",
            "Reported duration median (seconds, known only)": median(known)
            if known
            else "Unknown",
            "Reported duration minimum (seconds, known only)": min(known)
            if known
            else "Unknown",
            "Reported duration maximum (seconds, known only)": max(known)
            if known
            else "Unknown",
            "Duration distribution": "; ".join(
                f"{key}: {value}" for key, value in sorted(durations.items())
            )
            or "No calls",
            "Unknown event time in selection": sum(
                item.occurred_at is None for _, item in rows
            ),
            "Missing event time before period filter": missing_times,
            "Recorded UTC hour counts": "; ".join(
                f"{hour:02d}:00–{hour:02d}:59: {count}"
                for hour, count in sorted(hours.items())
            )
            or "No known call times",
            "Participant pair counts": "; ".join(
                f"{pair_labels[pair]} [{', '.join(pair)}]: {count}"
                for pair, count in sorted(pairs.items())
            )
            or "No calls",
        }
    )
    limitations = [
        "Counts describe retained records, not network-verified activity. Unknown "
        "duration differs from zero; these records do not report answered/missed "
        "status or verify who participated or appeared on camera."
    ]
    if (start is not None or end is not None) and missing_times:
        warnings.append(
            "Calls without an event time are excluded by the selected period."
        )
    return {
        "summary": (
            f"{len(rows)} selected call records, with {len(known)} known "
            "reported durations. Recorded hour counts use UTC."
        ),
        "metrics": metrics,
        "items": items,
        "warnings": warnings,
        "limitations": limitations,
    }

"""Neutral bounded-query and presentation helpers for current-field reports."""

from datetime import UTC

from django.urls import reverse

MAX_RECORDS = 500


def _url(case, kind, item):
    return reverse(
        "core:evidence:detail",
        kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
    )


def _instant(value):
    return value.astimezone(UTC).isoformat() if value is not None else "Unknown"


def _state(queryset, state):
    if state == "all":
        return queryset
    return queryset.filter(is_archived=state == "archived")


def _period(queryset, field, start, end):
    if start is not None:
        queryset = queryset.filter(**{f"{field}__gte": start})
    if end is not None:
        queryset = queryset.filter(**{f"{field}__lte": end})
    return queryset


def _selection(querysets, limit):
    """Bound materialization across the small explicit set of source tables."""
    available = 0
    rows = []
    for kind, queryset in querysets:
        available += queryset.count()
        rows.extend(
            (kind, item) for item in queryset.order_by("created_at", "pk")[:limit]
        )
    rows.sort(key=lambda row: (row[1].created_at, row[0], str(row[1].pk)))
    rows = rows[:limit]
    warnings = []
    if available > len(rows):
        warnings.append(
            f"Only the first {len(rows)} of {available} matching records by upload "
            "time are included. Narrow the selection to inspect the rest. "
            "Counts and findings describe this bounded selection."
        )
    return rows, available, warnings


def _base_metrics(rows, available, state, start, end, time_field):
    return {
        "Matching records": available,
        "Selected records": len(rows),
        "Truncated": available > len(rows),
        "Archive filter": state,
        "Period field": time_field,
        "Period start (UTC, inclusive)": _instant(start)
        if start
        else "Not constrained",
        "Period end (UTC, inclusive)": _instant(end) if end else "Not constrained",
    }


def _source(case, kind, item, values, *, kind_label, text=""):
    return {
        "label": f"{kind_label} · {item.title}",
        "text": text,
        "url": _url(case, kind, item),
        "values": {
            "Archived": item.is_archived,
            "Collected at (UTC)": _instant(item.collected_at),
            "Uploaded at (UTC)": _instant(item.created_at),
            **values,
        },
    }

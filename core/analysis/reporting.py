"""Neutral bounded-query and presentation helpers for current-field reports."""

from datetime import UTC
from heapq import merge
from itertools import islice

from django.core.paginator import Paginator
from django.db.models import CharField, F, Value
from django.urls import reverse

BATCH_SIZE = 500
PAGE_SIZE = 25


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


def _record_page(querysets, page, page_size=PAGE_SIZE, *, time_field="created_at"):
    """Page database identities before loading any complete source records."""
    identities = [
        queryset.order_by()
        .annotate(
            report_kind=Value(kind, output_field=CharField()), report_at=F(time_field)
        )
        .values("pk", "report_at", "report_kind")
        for kind, queryset in querysets
    ]
    combined = (
        identities[0]
        .union(*identities[1:], all=True)
        .order_by(F("report_at").asc(nulls_last=True), "report_kind", "pk")
    )
    page_obj = Paginator(combined, page_size).get_page(page)
    references = list(page_obj)
    objects = {}
    for kind, queryset in querysets:
        ids = [row["pk"] for row in references if row["report_kind"] == kind]
        objects.update(((kind, item.pk), item) for item in queryset.filter(pk__in=ids))
    rows = [
        (row["report_kind"], objects[(row["report_kind"], row["pk"])])
        for row in references
        if (row["report_kind"], row["pk"]) in objects
    ]
    return page_obj, rows


def _median(querysets, field, *, value=lambda item: item):
    """Merge database-sorted scalar streams without retaining the population."""
    selected = [
        queryset.exclude(**{f"{field}__isnull": True}) for queryset in querysets
    ]
    count = sum(queryset.count() for queryset in selected)
    if not count:
        return None
    streams = [
        queryset.order_by(field, "pk")
        .values_list(field, flat=True)
        .iterator(chunk_size=BATCH_SIZE)
        for queryset in selected
    ]
    middle = list(islice(merge(*streams), (count - 1) // 2, count // 2 + 1))
    middle = [value(item) for item in middle]
    return middle[0] if len(middle) == 1 else (middle[0] + middle[1]) / 2


def _group_page(groups, page):
    """Page a merged aggregate stream; both passes retain only one group/page."""
    count = sum(1 for _ in groups())
    page_obj = Paginator(range(count), PAGE_SIZE).get_page(page)
    page_obj.object_list = list(
        islice(
            groups(), page_obj.start_index() - 1 if count else 0, page_obj.end_index()
        )
    )
    return page_obj


def _base_metrics(total, state, start, end, time_field):
    return {
        "Matching records": total,
        "Selected records": total,
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

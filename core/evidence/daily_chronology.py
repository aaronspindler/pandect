"""Read one local day of retained sources and validated groupings."""

from datetime import UTC, date, datetime, time, timedelta
from heapq import merge
from itertools import islice
from operator import itemgetter
from sys import maxsize

from django import forms
from django.db.models import F, Value
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from cases.permissions import case_context
from core.evidence.queries import scoped_evidence
from core.evidence.types import (
    CALL_KINDS,
    COMMUNICATION_KINDS,
    KIND_LABELS,
    KINDS,
    LOCATION_KINDS,
)
from geo.daily_chronology import (
    attach_daily_location_details,
    daily_location_rows,
    daily_trip_covers,
)
from phone.daily_chronology import attach_daily_message_previews, daily_message_rows

PAGE_SIZE = 25
DAY = timedelta(days=1)
ROW_LABELS = KIND_LABELS | {
    "conversation": "Conversation",
    "message": "Text message",
    "trip": "Possible trip",
    "collected": "Collected",
}


def _local_midnight(day):
    return timezone.make_aware(datetime.combine(day, time.min))


class DayForm(forms.Form):
    day = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))

    def clean_day(self):
        day = self.cleaned_data["day"]
        try:
            self.start = _local_midnight(day).astimezone(UTC)
            self.end = _local_midnight(day + DAY).astimezone(UTC)
        except OverflowError as error:
            raise forms.ValidationError(
                "Choose a date with a valid start and end in this timezone."
            ) from error
        return day


def timestamp_fields(kind, category):
    if category in ("all", "observed"):
        if kind in COMMUNICATION_KINDS:
            yield "occurred_at", "Occurred"
        elif kind in LOCATION_KINDS:
            yield "observed_at", "Observed"
    if category in ("all", "acquired"):
        yield "collected_at", "Collected"


def _timeline_day(stamp):
    day = timezone.localdate(stamp)
    # A change near midnight can make the wall date disagree with the
    # half-open interval used to load a timeline section.
    if day < date.max and stamp >= _local_midnight(day + DAY):
        return day + DAY
    if day > date.min and stamp < _local_midnight(day):
        return day - DAY
    return day


def _nearest_recorded_day(case, *, boundary=None, forward=False):
    # Each source contributes at most one timestamp, including on large imports.
    order = "stamp" if forward else "-stamp"
    lookup = "__gte" if forward else "__lt"
    branches = []
    for kind in KINDS:
        for field, _ in timestamp_fields(kind, "all"):
            query = scoped_evidence(case, kind).filter(**{field + "__isnull": False})
            if boundary is not None:
                query = query.filter(**{field + lookup: boundary})
            branches.append(
                query.annotate(stamp=F(field))
                .values_list("stamp", flat=True)
                .order_by(order)[:1]
            )
    stamp = branches[0].union(*branches[1:], all=True).order_by(order).first()
    return _timeline_day(stamp) if stamp else None


def _adjacent_populated_day(case, day, boundary, *, forward=False):
    recorded = _nearest_recorded_day(case, boundary=boundary, forward=forward)
    if recorded is None:
        return None
    # A day between the boundary and the nearest original can have entries
    # only if a validated trip spans the boundary into that day.
    instant = boundary if forward else boundary - timedelta.resolution
    adjacent = _timeline_day(instant)
    trip_day_is_closer = (
        day < adjacent < recorded if forward else recorded < adjacent < day
    )
    if trip_day_is_closer and daily_trip_covers(case, instant):
        return adjacent
    return recorded


def _collected(case, start, end):
    queries = [
        scoped_evidence(case, kind)
        .filter(collected_at__gte=start, collected_at__lt=end)
        .annotate(source_kind=Value(kind))
        .order_by()
        .values("id", "collected_at", "source_kind", "title", "is_archived")
        for kind in KINDS
    ]
    for row in (
        queries[0]
        .union(*queries[1:], all=True)
        .order_by("collected_at", "source_kind", "id")
        .iterator(chunk_size=PAGE_SIZE)
    ):
        source_kind = row["source_kind"]
        yield {
            "at": row["collected_at"],
            "kind": "collected",
            "id": row["id"],
            "source_kind": source_kind,
            "title": row["title"],
            "detail": KIND_LABELS[source_kind],
            "archived": row["is_archived"],
        }


def _calls(case, kind, start, end):
    calls = (
        scoped_evidence(case, kind)
        .filter(occurred_at__gte=start, occurred_at__lt=end)
        .select_related("sender", "recipient")
        .order_by("occurred_at", "pk")
    )
    for call in calls.iterator(chunk_size=PAGE_SIZE):
        yield {
            "at": call.occurred_at,
            "kind": kind,
            "id": call.pk,
            "call": call,
            "archived": call.is_archived,
        }


def _day_page(case, start, end, number):
    entries = merge(
        daily_message_rows(case, start, end),
        daily_location_rows(case, start, end),
        *(_calls(case, kind, start, end) for kind in CALL_KINDS),
        _collected(case, start, end),
        key=itemgetter("at"),
    )
    offset = (number - 1) * PAGE_SIZE
    rows = list(islice(entries, offset, offset + PAGE_SIZE + 1))
    if not rows and number > 1:
        raise Http404("Timeline page unavailable.")
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    attach_daily_location_details(case, rows)
    attach_daily_message_previews(case, rows)
    for row in rows:
        if call := row.pop("call", None):
            row["title"] = f"{call.sender} to {call.recipient}"
            row["detail"] = (
                f"Duration {call.duration}" if call.duration is not None else ""
            )
        if "url" not in row:
            row["url"] = reverse(
                "core:evidence:detail",
                args=(case.pk, row.get("source_kind", row["kind"]), row["id"]),
                query={"state": "all"},
            )
    return rows, more


def daily_chronology(request, case):
    account_timezone = timezone.get_current_timezone_name()
    show_utc = request.GET.get("clock") == "utc"
    if "day" not in request.GET:
        day = _nearest_recorded_day(case) or timezone.localdate()
        query = {"view": "day", "day": day.isoformat()}
        if show_utc:
            query["clock"] = "utc"
        return redirect(
            reverse("core:evidence:chronology", args=(case.pk,), query=query)
        )

    form = DayForm(request.GET)
    context = {
        **case_context(case, request.user),
        "nav_section": "chronology",
        "form": form,
        "account_timezone": account_timezone,
        "display_timezone": "UTC" if show_utc else account_timezone,
        "show_utc": show_utc,
        "rows": [],
    }
    if not form.is_valid():
        return render(
            request, "core/evidence/daily_chronology.html", context, status=400
        )

    day = form.cleaned_data["day"]
    try:
        page = int(request.GET.get("page", 1))
    except ValueError:
        raise Http404("Timeline page unavailable.") from None
    if not 1 <= page < maxsize // PAGE_SIZE:
        raise Http404("Timeline page unavailable.")
    rows, more = _day_page(case, form.start, form.end, page)
    for row in rows:
        row["label"] = ROW_LABELS[row["kind"]]
        row.setdefault("start_at", row["at"])
    context.update(
        day=day,
        rows=rows,
        previous_day=_adjacent_populated_day(case, day, form.start),
        next_day=_adjacent_populated_day(case, day, form.end, forward=True),
        page_number=page,
        previous_page=page - 1 if page > 1 else None,
        next_page=page + 1 if more else None,
    )
    return render(request, "core/evidence/daily_chronology.html", context)

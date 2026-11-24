"""Read saved possible trips and uncovered observations for one case day."""

from heapq import merge
from operator import itemgetter

from django.db.models import Count, Exists, F, Max, Min, OuterRef
from django.db.models.functions import Greatest

from core.evidence.queries import scoped_evidence
from core.evidence.types import LOCATION_KINDS
from geo.trip_models import VehicleTripSource
from geo.trip_processing import format_trips, saved_trips


def _members(case):
    return VehicleTripSource.objects.filter(
        case=case, input__case=case, input__generation_id=F("trip__generation_id")
    )


def _complete_trips(case):
    # Only collapse observations when the full, distinct set of current originals
    # still supports the saved interval. Incomplete trips leave their sources visible.
    complete = (
        _members(case)
        .filter(trip_id=OuterRef("pk"))
        .order_by()
        .values("trip_id")
        .annotate(
            count=Count("pk"),
            originals=Count("input_id", distinct=True),
            first=Min("input__observation__observed_at"),
            last=Max("input__observation__observed_at"),
        )
        .filter(
            count=OuterRef("source_count"),
            originals=OuterRef("source_count"),
            first=OuterRef("start_at"),
            last=OuterRef("end_at"),
        )
    )
    return saved_trips(case).filter(Exists(complete))


def daily_trip_covers(case, instant):
    return (
        _complete_trips(case)
        .filter(start_at__lte=instant, end_at__gte=instant)
        .exists()
    )


def _trip_rows(trips, start, end):
    ordered = trips.annotate(day_at=Greatest("start_at", start)).order_by(
        "day_at", "pk"
    )
    for trip in ordered.iterator(chunk_size=25):
        yield {
            "at": trip.day_at,
            "kind": "trip",
            "id": trip.pk,
            "trip": trip,
            "start_at": trip.start_at,
            "end_at": trip.end_at,
            "continues_before": trip.start_at < start,
            "continues_after": trip.end_at >= end,
        }


def _observation_rows(case, kind, start, end, trips):
    records = scoped_evidence(case, kind).filter(
        is_archived=False, observed_at__gte=start, observed_at__lt=end
    )
    fields = [
        "pk",
        "observed_at",
        "title",
        "address",
        "latitude",
        "longitude",
    ]
    if kind == "person-location":
        records = records.select_related("person")
        fields.extend(
            (
                "person__first_name",
                "person__last_name",
                "person__email",
                "person__phone_number",
            )
        )
    elif kind == "vehicle-location":
        covered = (
            _members(case)
            .filter(trip__in=trips.order_by())
            .values("input__observation_id")
        )
        records = records.exclude(pk__in=covered).select_related("vehicle")
        fields.extend(
            (
                "vehicle__make",
                "vehicle__model",
                "vehicle__year",
                "vehicle__license_plate",
                "vehicle__vin",
            )
        )
    for record in (
        records.only(*fields).order_by("observed_at", "pk").iterator(chunk_size=25)
    ):
        yield {
            "at": record.observed_at,
            "kind": kind,
            "id": record.pk,
            "title": record.title,
            "observation": record,
        }


def daily_location_rows(case, start, end):
    """Merge sortable entries in [start, end), retaining full trip endpoints."""
    if start == end:  # A change of timezone offset can skip an entire civil date.
        return iter(())
    trips = _complete_trips(case).filter(end_at__gte=start, start_at__lt=end)
    return merge(
        _trip_rows(trips, start, end),
        *(_observation_rows(case, kind, start, end, trips) for kind in LOCATION_KINDS),
        key=itemgetter("at", "kind", "id"),
    )


def attach_daily_location_details(case, rows):
    trip_rows = [row for row in rows if row["kind"] == "trip"]
    if trip_rows:
        trips = [row.pop("trip") for row in trip_rows]
        for row, trip, summary in zip(
            trip_rows, trips, format_trips(case, trips), strict=True
        ):
            detail = (
                f"{summary['start']['place']} → {summary['end']['place']} · "
                f"{trip.source_count} observations"
            )
            row.update(title=summary["vehicle"], detail=detail, url=summary["trip_url"])
    for row in rows:
        if record := row.pop("observation", None):
            place = record.address.strip() or f"{record.latitude}, {record.longitude}"
            subject = getattr(record, "person", None) or getattr(
                record, "vehicle", None
            )
            row["detail"] = f"{subject} · {place}" if subject else place

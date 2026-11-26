"""Read published possible trips, optionally anchored to a message or observation."""

import re
from datetime import datetime, timedelta

from django.db.models import F, Q
from django.db.models.functions import Greatest
from django.urls import reverse

from core.evidence.source_clocks import is_date_only_message
from geo.trip_citations import citation_fields, citation_inputs, citation_trips

from . import corpus

MAX_TRIPS = 3


def _material(case, trip, start, size):
    inputs = citation_inputs(case, trip)
    if not inputs:
        return None
    material = corpus.make_material(
        "trip",
        "possible-trip",
        None,
        trip,
        [corpus.dependency("vehicle-location", item.observation_id) for item in inputs],
        f"Saved possible trip · {trip.vehicle}",
        reverse("geo:trip_citation", args=(case.pk, trip.pk)),
        version=[
            str(trip.generation_id),
            trip.generation.method_version,
            [[str(item.pk), item.sha256] for item in inputs],
        ],
        suffix=corpus.lines(citation_fields(trip, inputs)),
        start=start,
        size=size,
    )
    material.locator["start_at"] = trip.start_at.isoformat()
    material.locator["end_at"] = trip.end_at.isoformat()
    return material


def read_trip_material(case, trip_id, start, size, *, through=None):
    trips = citation_trips(case, retained=True).filter(pk=trip_id)
    if through:
        trips = trips.filter(generation__finished_at__lte=through)
    trip = trips.first()
    return _material(case, trip, start, size) if trip else None


def trip_materials(
    case, query, *, through, source, filters, after=None, prior_trips=None
):
    if filters.get("record_type", "vehicle-location") != "vehicle-location":
        return []
    if (
        filters.get("start")
        and filters.get("end")
        and filters["start"] >= filters["end"]
    ):
        return []
    trips = citation_trips(case).filter(generation__finished_at__lte=through)
    if after:
        trips = trips.filter(generation__finished_at__gt=after)
    if prior_trips is not None:
        vehicles = citation_trips(case, retained=True).filter(pk__in=prior_trips)
        trips = trips.filter(vehicle_id__in=vehicles.order_by().values("vehicle_id"))
    if start := filters.get("start"):
        trips = trips.filter(end_at__gte=start)
    if end := filters.get("end"):
        trips = trips.filter(start_at__lt=end)
    if source:
        kind = source["kind"]
        if source["surface"] != "original" or kind not in (
            "vehicle-location",
            "text-message",
        ):
            return []
        originals = corpus.rows(case, "original", kind, False, through)
        if kind == "vehicle-location":
            trips = trips.filter(
                sources__case=case,
                sources__input__case=case,
                sources__input__generation_id=F("generation_id"),
                sources__input__observation__in=originals.filter(pk=source["pk"]),
            )
        else:
            stamp = (
                originals.exclude(is_date_only_message(case))
                .filter(pk=source["pk"])
                .values_list("occurred_at", flat=True)
                .first()
            )
            if stamp is None:
                return []
            margin = timedelta(hours=1)
            trips = trips.filter(
                start_at__lte=stamp + margin, end_at__gte=stamp - margin
            )
            trips = trips.order_by(
                Greatest(F("start_at") - stamp, stamp - F("end_at"), timedelta()),
                "start_at",
                "pk",
            )
    elif query.strip().casefold() != "recent":
        # A named search should not broaden to other vehicles when there is no match.
        words = query.split()[:6]
        for word in words:
            trips = trips.filter(
                Q(vehicle__make__icontains=word)
                | Q(vehicle__model__icontains=word)
                | Q(vehicle__license_plate__iexact=word)
                | Q(vehicle__vin__iexact=word)
            )
    return [
        material
        for trip in trips[:MAX_TRIPS]
        if (material := _material(case, trip, 0, corpus.MAX_WINDOW))
    ]


def trip_links(case, locator, question, through, filters):
    """Follow a verified interval to its endpoints and nearby timed messages."""
    from .search_scope import scope_rows

    inputs = locator["dependencies"]
    for source in (inputs[0], inputs[-1]):
        yield "original", source["kind"], source["id"]
    if re.search(r"\b(?:messages?|texts?|chats?|conversations?)\b", question, re.I):
        messages = scope_rows(
            corpus.rows(case, "original", "text-message", False, through),
            case,
            "original",
            "text-message",
            filters,
        ).exclude(is_date_only_message(case))
        for field in ("start_at", "end_at"):
            stamp = datetime.fromisoformat(locator[field])
            matching = messages.filter(
                occurred_at__gte=stamp - timedelta(hours=1),
                occurred_at__lte=stamp + timedelta(hours=1),
            )
            nearest = []
            for lookup, order in (("gte", "occurred_at"), ("lt", "-occurred_at")):
                row = (
                    matching.filter(**{f"occurred_at__{lookup}": stamp})
                    .order_by(order, "pk")
                    .values_list("occurred_at", "pk")
                    .first()
                )
                if row:
                    nearest.append((abs(row[0] - stamp), row[1]))
            if nearest:
                yield "original", "text-message", str(min(nearest)[1])
    else:
        for source in inputs[1:-1]:
            yield "original", source["kind"], source["id"]

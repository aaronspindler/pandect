"""Live, case-scoped selections. Search rows point to the canonical readers."""

from dataclasses import dataclass

from django.db.models import Case, CharField, F, Q, Value, When
from django.db.models.functions import Coalesce, Concat, Greatest, Least, NullIf, Trim

from core.analysis.catalogue import finding_queryset
from core.evidence.queries import scoped_evidence
from core.evidence.types import (
    COMMUNICATION_KINDS,
    FILE_KINDS,
    KIND_LABELS,
    KINDS,
    LOCATION_KINDS,
)
from geo.models import PointOfInterest
from geo.trip_processing import saved_trips
from people.models import Person
from vehicle.models import Vehicle

LABELS = {
    **KIND_LABELS,
    "person": "Person",
    "vehicle": "Vehicle",
    "poi": "Point of interest",
    "conversation": "Conversation",
    "trip": "Possible trip",
    "finding": "Finding",
}
TYPE_CHOICES = tuple(LABELS.items())


def name(prefix=""):
    def field(key):
        return F(prefix + key)

    return Coalesce(
        NullIf(
            Trim(Concat(field("first_name"), Value(" "), field("last_name"))), Value("")
        ),
        NullIf(field("email"), Value("")),
        field("phone_number"),
        output_field=CharField(),
    )


def vehicle_name(prefix=""):
    return Coalesce(
        NullIf(
            Trim(Concat(F(prefix + "make"), Value(" "), F(prefix + "model"))), Value("")
        ),
        NullIf(F(prefix + "license_plate"), Value("")),
        F(prefix + "vin"),
        output_field=CharField(),
    )


@dataclass
class Source:
    queryset: object
    title: object
    date_fields: tuple
    identifiers: tuple = ()


def sources(case, kind):
    if kind in KINDS:
        qs = scoped_evidence(case, kind).filter(is_archived=False)
        dates = (("collected_at", "Collected"), ("created_at", "Added"))
        if kind in COMMUNICATION_KINDS:
            dates = (("occurred_at", "Occurred"), *dates)
        elif kind in LOCATION_KINDS:
            dates = (("observed_at", "Observed"), *dates)
        ids = (
            "title",
            "sha256",
            *(("original_filename",) if kind in FILE_KINDS else ()),
        )
        return Source(qs, F("title"), dates, ids)
    if kind == "conversation":
        qs = scoped_evidence(case, "text-message").filter(
            is_archived=False, recipient_id__isnull=False
        )
        qs = qs.alias(
            pair_a=Least("sender_id", "recipient_id"),
            pair_b=Greatest("sender_id", "recipient_id"),
        )
        return Source(
            qs,
            Concat(name("sender__"), Value(" · "), name("recipient__")),
            (("occurred_at", "Message occurred"), ("created_at", "Message added")),
        )
    if kind == "trip":
        return Source(
            saved_trips(case),
            Concat(Value("Possible trip · "), vehicle_name("vehicle__")),
            (("start_at", "Trip started"),),
        )
    if kind == "finding":
        return Source(
            finding_queryset(case, {}),
            F("label"),
            (("result__run__created_at", "Analysis requested"),),
        )
    dates = (("created_at", "Added"),)
    if kind == "person":
        return Source(
            Person.objects.filter(case=case),
            name(),
            dates,
            ("email", "phone_number", "first_name", "last_name"),
        )
    if kind == "vehicle":
        return Source(
            Vehicle.objects.filter(case=case),
            vehicle_name(),
            dates,
            ("license_plate", "vin"),
        )
    qs = PointOfInterest.objects.filter(person__case=case)
    label = Case(
        When(kind="custom", then="custom_label"),
        When(kind="home", then=Value("Home")),
        default=Value("Work"),
        output_field=CharField(),
    )
    return Source(qs, Concat(label, Value(" · "), name("person__")), dates)


def dated(source, start, end, kind):
    dates = source.date_fields
    date = (
        F(dates[0][0]) if len(dates) == 1 else Coalesce(*(field for field, _ in dates))
    )
    date_label = Value(dates[-1][1])
    if len(dates) > 1:
        date_label = Case(
            *(
                When(**{field + "__isnull": False}, then=Value(label))
                for field, label in dates[:-1]
            ),
            default=date_label,
        )
    qs = source.queryset.annotate(
        hit_date=date, hit_date_label=date_label, hit_title=source.title
    )
    if kind == "trip":
        if start:
            qs = qs.filter(end_at__gte=start)
        if end:
            qs = qs.filter(hit_date__lt=end)
    elif start or end:
        # Compare the first available date using its own column so timestamp
        # indexes remain usable. Later dates only apply when earlier ones lack data.
        preferred = Q()
        bounds = Q(pk__in=[])
        for field, _ in dates:
            limits = {}
            if start:
                limits[f"{field}__gte"] = start
            if end:
                limits[f"{field}__lt"] = end
            bounds |= preferred & Q(**limits)
            preferred &= Q(**{f"{field}__isnull": True})
        qs = qs.filter(bounds)
    return qs

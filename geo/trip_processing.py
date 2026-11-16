"""Authorized scheduling and current saved-trip readers."""

from datetime import datetime
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

from cases.permissions import case_role, get_case
from core.evidence.queries import scoped_evidence
from geo.trip_models import (
    VehicleTrip,
    VehicleTripGeneration,
    VehicleTripInput,
    VehicleTripState,
)
from vehicle.models import Vehicle

METHOD_VERSION = "vehicle-trips.v1"
EVIDENCE_STATES = ("active", "archived", "all")


def _verified_editor(actor, case):
    return (
        case_role(case, actor) in ("owner", "editor")
        and EmailAddress.objects.filter(user=actor, verified=True).exists()
    )


def _vehicles(case, identifiers):
    queryset = Vehicle.objects.filter(case=case)
    if identifiers is not None:
        try:
            wanted = {str(identifier) for identifier in identifiers}
            queryset = queryset.filter(pk__in=wanted)
            if set(str(pk) for pk in queryset.values_list("pk", flat=True)) != wanted:
                raise Http404("Vehicle not found.")
        except ValueError, ValidationError:
            raise Http404("Vehicle not found.") from None
    else:
        queryset = queryset.filter(
            Q(location_evidence__isnull=False) | Q(vehicletripstate__isnull=False)
        ).distinct()
    return queryset


def _enqueue(actor, state, *, previous=None):
    attempts = previous.attempts + 1 if previous else 1
    if attempts > 3:
        raise ValidationError(
            "Trip processing reached its three-attempt limit. "
            "Change the evidence or ask an operator to investigate."
        )
    generation = VehicleTripGeneration.objects.create(
        case_id=state.case_id,
        vehicle_id=state.vehicle_id,
        state=state,
        revision=state.revision,
        method_version=METHOD_VERSION,
        requested_by=actor,
        previous_generation=previous,
        attempts=attempts,
        total_sources=scoped_evidence(state.case, "vehicle-location")
        .filter(vehicle_id=state.vehicle_id)
        .count(),
        metrics={
            name: {
                "observations": 0,
                "missing_times": 0,
                "conflicts": 0,
                "incomparable": 0,
                "trips": 0,
                "gaps": 0,
            }
            for name in EVIDENCE_STATES
        },
    )
    from geo.tasks import process_vehicle_trips

    process_vehicle_trips.enqueue(str(generation.pk), str(generation.token))
    return generation


@transaction.atomic
def invalidate_vehicle_trips(actor, case, vehicle_ids):
    """Invalidate after an authorized write; only verified editors queue rebuilding.

    A superuser viewer may delete evidence with current case access. In that case
    the old generation is invalidated and a case editor can prepare its replacement.
    Call within the source mutation transaction so invalidation and queueing commit
    with that mutation.
    """
    case = get_case(actor, case.pk, lock=True)
    if case_role(case, actor) not in ("owner", "editor") and not actor.is_superuser:
        raise PermissionDenied(
            "Only case editors or a superuser performing an authorized deletion "
            "may invalidate trips."
        )
    vehicles = list(_vehicles(case, vehicle_ids).order_by("pk"))
    can_process = _verified_editor(actor, case)
    generations = []
    for vehicle in vehicles:
        state, _ = VehicleTripState.objects.get_or_create(case=case, vehicle=vehicle)
        state = VehicleTripState.objects.select_for_update().get(pk=state.pk)
        state.revision += 1
        state.current_generation = None
        state.save(update_fields=("revision", "current_generation"))
        state.generations.filter(status__in=("queued", "running")).update(
            status="superseded", finished_at=timezone.now()
        )
        state.generations.filter(
            status="complete", geocode_status__in=("pending", "queued", "running")
        ).update(geocode_status="superseded", token=uuid4())
        if can_process:
            generations.append(_enqueue(actor, state))
    return generations


@transaction.atomic
def prepare_case_trips(
    actor, case_id, *, vehicle_ids=None, retry=False, worker_stopped=False
):
    """Explicit, idempotent initialization/recovery; safe GET readers never enqueue."""
    case = get_case(actor, case_id, edit=True, lock=True)
    if not _verified_editor(actor, case):
        raise PermissionDenied("Verify your email before processing trips.")
    generations = []
    for vehicle in _vehicles(case, vehicle_ids).order_by("pk"):
        state, _ = VehicleTripState.objects.get_or_create(case=case, vehicle=vehicle)
        state = VehicleTripState.objects.select_for_update().get(pk=state.pk)
        latest = state.generations.filter(
            revision=state.revision, method_version=METHOD_VERSION
        ).first()
        if latest and latest.status in ("queued", "running", "complete"):
            if latest.status in ("queued", "running") and retry:
                if latest.status == "running" and not worker_stopped:
                    raise ValidationError(
                        "Confirm the original worker has stopped before recovering "
                        "running trip processing."
                    )
                latest.status = "superseded"
                latest.finished_at = timezone.now()
                latest.save(update_fields=("status", "finished_at"))
                generations.append(_enqueue(actor, state, previous=latest))
            else:
                if latest.status == "complete":
                    _queue_geocoding(
                        actor, latest, retry=retry, worker_stopped=worker_stopped
                    )
                generations.append(latest)
            continue
        if latest and latest.status == "failed" and not retry:
            generations.append(latest)
            continue
        generations.append(
            _enqueue(
                actor,
                state,
                previous=latest if latest and latest.status == "failed" else None,
            )
        )
    return generations


def _queue_geocoding(actor, generation, *, retry=False, worker_stopped=False):
    from geo.map_geocoding import local_geocoder_ready

    if not local_geocoder_ready():
        generation.geocode_status = "disabled"
        return
    if generation.geocode_status == "complete":
        return
    if generation.geocode_status == "queued" and not retry:
        return
    if generation.geocode_status in ("failed", "running") and not retry:
        return
    if generation.geocode_status == "running" and not worker_stopped:
        raise ValidationError(
            "Confirm the old worker stopped before recovering address preparation."
        )
    if generation.geocode_attempts >= 3:
        raise ValidationError("Address preparation reached its three-attempt limit.")
    generation.geocode_attempts += 1
    generation.geocode_requested_by = actor
    generation.geocode_status = "queued"
    generation.geocode_error_code = ""
    generation.token = uuid4()
    generation.save(
        update_fields=(
            "geocode_attempts",
            "geocode_requested_by",
            "geocode_status",
            "geocode_error_code",
            "token",
        )
    )
    from geo.tasks import prepare_trip_addresses

    prepare_trip_addresses.enqueue(str(generation.pk), str(generation.token))


def current_generations(case, *, vehicle_id=None):
    """Reject stale or corrupted inputs before exposing any saved interpretation."""
    valid_originals = scoped_evidence(case, "vehicle-location").values("pk")
    inputs = VehicleTripInput.objects.filter(generation_id=OuterRef("pk"))
    invalid = inputs.annotate(
        archived_snapshot=Cast(
            KeyTextTransform("is_archived", "snapshot"), models.BooleanField()
        ),
    ).filter(
        ~Q(case_id=case.pk)
        | ~Q(observation_id__in=valid_originals)
        | ~Q(observation__vehicle_id=F("generation__vehicle_id"))
        | ~Q(observation__sha256=F("sha256"))
        | ~Q(archived_snapshot=F("observation__is_archived"))
    )
    generations = (
        VehicleTripGeneration.objects.filter(
            case=case,
            vehicle__case=case,
            state__case=case,
            state__vehicle_id=F("vehicle_id"),
            state__current_generation_id=F("pk"),
            state__revision=F("revision"),
            status="complete",
            method_version=METHOD_VERSION,
        )
        .annotate(invalid_inputs=Exists(invalid))
        .filter(invalid_inputs=False)
    )
    if vehicle_id:
        _vehicles(case, [vehicle_id])
        generations = generations.filter(vehicle_id=vehicle_id)
    return generations


def trip_readiness(case, *, vehicle_id=None):
    """Shared preparation/status decisions without writes or automatic retries."""
    sources = scoped_evidence(case, "vehicle-location")
    states = VehicleTripState.objects.filter(case=case, vehicle__case=case)
    if vehicle_id:
        sources = sources.filter(vehicle_id=vehicle_id)
        states = states.filter(vehicle_id=vehicle_id)
    latest = VehicleTripGeneration.objects.filter(
        state_id=OuterRef("pk"),
        revision=OuterRef("revision"),
        method_version=METHOD_VERSION,
    ).values("pk")[:1]
    generations = VehicleTripGeneration.objects.filter(
        case=case,
        state__case=case,
        vehicle__case=case,
        state__vehicle_id=F("vehicle_id"),
        pk__in=states.annotate(latest_id=Subquery(latest)).values("latest_id"),
    )
    complete = current_generations(case, vehicle_id=vehicle_id)
    missing = sources.exclude(vehicle_id__in=complete.values("vehicle_id"))
    needs_prepare = missing.exclude(
        vehicle_id__in=generations.filter(
            status__in=("queued", "running", "failed", "complete")
        ).values("vehicle_id")
    ).exists()
    from geo.map_geocoding import local_geocoder_ready

    if local_geocoder_ready():
        needs_prepare = (
            needs_prepare
            or complete.filter(geocode_status__in=("disabled", "pending")).exists()
        )
    return {
        "pending": generations.filter(status__in=("queued", "running")).exists(),
        "failed": (
            generations.filter(status="failed").exists()
            or generations.filter(status="complete")
            .exclude(pk__in=complete.values("pk"))
            .exists()
        ),
        "needs_prepare": needs_prepare,
    }


def saved_trips(
    case, *, state="active", vehicle_id=None, start=None, end=None, kind="trip"
):
    """Select complete saved trips overlapping a period, without cutting samples."""
    if state not in EVIDENCE_STATES or kind not in ("trip", "gap"):
        raise ValidationError("Choose a supported trip selection.")
    for value in (start, end):
        if value is not None and timezone.is_naive(value):
            raise ValidationError("Trip times require an explicit timezone.")
    if start is not None and end is not None and start > end:
        raise ValidationError("The period end must not precede its start.")
    query = VehicleTrip.objects.filter(
        case=case,
        vehicle__case=case,
        evidence_state=state,
        kind=kind,
        generation__in=current_generations(case, vehicle_id=vehicle_id),
        generation__vehicle_id=F("vehicle_id"),
    ).select_related("vehicle", "generation")
    if start is not None:
        query = query.filter(end_at__gte=start)
    if end is not None:
        query = query.filter(start_at__lte=end)
    return query.order_by("-start_at", "pk")


def trip_sources(case, trip):
    if (
        not saved_trips(case, state=trip.evidence_state, kind=trip.kind)
        .filter(pk=trip.pk)
        .exists()
    ):
        raise Http404("Trip not found.")
    return _source_rows(case, trip)


def _source_rows(case, trip):
    """Select sources after the caller has selected a current, authorized trip."""
    if (
        trip.case_id != case.pk
        or trip.vehicle.case_id != case.pk
        or trip.generation.case_id != case.pk
        or trip.generation.vehicle_id != trip.vehicle_id
    ):
        raise Http404("Trip not found.")
    query = VehicleTripInput.objects.filter(
        case=case,
        generation_id=trip.generation_id,
        memberships__case=case,
        memberships__trip=trip,
        memberships__trip__case=case,
    ).order_by("memberships__ordinal")
    if query.count() != trip.source_count:
        raise Http404("Trip sources are unavailable.")
    return query


def format_source(case, source):
    snapshot = source.snapshot
    return {
        "id": str(source.observation_id),
        "input_id": str(source.pk),
        "label": snapshot["title"],
        "place": snapshot["address"]
        or f"{snapshot['latitude']}, {snapshot['longitude']}",
        "time": datetime.fromisoformat(snapshot["observed_at"])
        if snapshot["observed_at"]
        else None,
        "url": reverse(
            "core:evidence:detail",
            args=(case.pk, "vehicle-location", source.observation_id),
        ),
        "row": snapshot["source_row"],
        "import_url": reverse(
            "geo:vehicle_import_review", args=(case.pk, snapshot["import_id"])
        )
        if snapshot["import_id"]
        else "",
        "latitude": snapshot["latitude"],
        "longitude": snapshot["longitude"],
        "sha256": source.sha256,
    }


def format_trip(
    case, trip, *, include_sources=False, source_limit=100, source_offset=0
):
    """Present a trip selected through saved_trips in an already authorized case."""
    sources = _source_rows(case, trip)
    first, last = sources.first(), sources.last()
    if first is None or last is None:
        raise Http404("Trip sources are unavailable.")
    start, end = format_source(case, first), format_source(case, last)
    listed = (
        [
            format_source(case, source)
            for source in sources[
                source_offset : source_offset + min(500, max(1, source_limit))
            ]
        ]
        if include_sources
        else []
    )
    trip_url = reverse("geo:trip_detail", args=(case.pk, trip.pk))
    return {
        "id": str(trip.pk),
        "generation_id": str(trip.generation_id),
        "label": "Possible trip" if trip.kind == "trip" else "Coverage gap",
        "text": "Movement suggested by the retained observations.",
        "url": trip_url,
        "trip_url": trip_url,
        "map_url": trip_url,
        "vehicle": str(trip.vehicle),
        "vehicle_url": reverse("vehicle:detail", args=(case.pk, trip.vehicle_id)),
        "start": start,
        "end": end,
        "minutes": round((trip.end_at - trip.start_at).total_seconds() / 60, 1),
        "basis": trip.basis,
        "sources": listed,
        "related": listed,
        "source_count": trip.source_count,
        "values": {
            "Vehicle": str(trip.vehicle),
            "Observations": trip.source_count,
            "Largest sampling interval (minutes)": round(
                trip.largest_gap_seconds / 60, 1
            ),
        },
    }

"""Authorized scheduling and current saved-trip readers."""

from collections import defaultdict
from datetime import datetime
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction
from django.db.models import Count, Exists, F, Max, Min, OuterRef, Q, Subquery, Window
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
    VehicleTripSource,
    VehicleTripState,
)
from vehicle.models import Vehicle

METHOD_VERSION = "vehicle-trips.v2"
READABLE_METHODS = ("vehicle-trips.v1", METHOD_VERSION)


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
            Q(location_evidence__is_archived=False) | Q(vehicletripstate__isnull=False)
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
        total_sources=_source_count(state),
        metrics={
            "active": {
                "observations": 0,
                "missing_times": 0,
                "conflicts": 0,
                "incomparable": 0,
                "trips": 0,
                "gaps": 0,
            }
        },
    )
    from geo.tasks import process_vehicle_trips

    process_vehicle_trips.enqueue(str(generation.pk), str(generation.token))
    return generation


def _source_count(state):
    return (
        scoped_evidence(state.case, "vehicle-location")
        .filter(vehicle_id=state.vehicle_id, is_archived=False)
        .count()
    )


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
        pending = state.generations.filter(status__in=("queued", "running"))
        waiting = (
            pending.select_for_update()
            .filter(
                status="queued",
                started_at__isnull=True,
                processed_sources=0,
                method_version=METHOD_VERSION,
                attempts=1,
            )
            .first()
            if can_process
            else None
        )
        if waiting is not None:
            pending = pending.exclude(pk=waiting.pk)
        pending.update(status="superseded", finished_at=timezone.now())
        state.generations.filter(
            status="complete", geocode_status__in=("pending", "queued", "running")
        ).update(geocode_status="superseded", token=uuid4())
        if waiting is not None:
            # No work or reviewed inputs exist yet. The queued token can claim
            # the newest revision instead of adding a superseded delivery.
            waiting.revision = state.revision
            waiting.requested_by = actor
            waiting.total_sources = _source_count(state)
            state.generations.filter(pk=waiting.pk).update(
                revision=waiting.revision,
                requested_by=actor,
                total_sources=waiting.total_sources,
            )
            generations.append(waiting)
        elif can_process:
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


def _current_generation_states(case):
    return VehicleTripGeneration.objects.filter(
        case=case,
        vehicle__case=case,
        state__case=case,
        state__vehicle_id=F("vehicle_id"),
        state__current_generation_id=F("pk"),
        state__revision=F("revision"),
        status="complete",
        method_version__in=READABLE_METHODS,
    )


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
        _current_generation_states(case)
        .annotate(invalid_inputs=Exists(invalid))
        .filter(invalid_inputs=False)
    )
    if vehicle_id:
        _vehicles(case, [vehicle_id])
        generations = generations.filter(vehicle_id=vehicle_id)
    return generations


def latest_generations(case, *, vehicle_id=None):
    """Latest preparation attempt for each vehicle's current source revision."""
    states = VehicleTripState.objects.filter(case=case, vehicle__case=case)
    if vehicle_id:
        states = states.filter(vehicle_id=vehicle_id)
    latest = VehicleTripGeneration.objects.filter(
        state_id=OuterRef("pk"),
        revision=OuterRef("revision"),
        method_version=METHOD_VERSION,
    ).values("pk")[:1]
    return VehicleTripGeneration.objects.filter(
        case=case,
        state__case=case,
        vehicle__case=case,
        state__vehicle_id=F("vehicle_id"),
        pk__in=states.annotate(latest_id=Subquery(latest)).values("latest_id"),
    )


def trip_readiness(case, *, vehicle_id=None):
    """Shared preparation/status decisions without writes or automatic retries."""
    sources = scoped_evidence(case, "vehicle-location").filter(is_archived=False)
    if vehicle_id:
        sources = sources.filter(vehicle_id=vehicle_id)
    generations = latest_generations(case, vehicle_id=vehicle_id)
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


def _trips(case, generations, kind):
    return VehicleTrip.objects.filter(
        case=case,
        vehicle__case=case,
        evidence_state="active",
        kind=kind,
        generation__in=generations,
        generation__vehicle_id=F("vehicle_id"),
    ).select_related("vehicle", "generation")


def geometry_trips(case):
    """Select published trips whose geometry is checked one page at a time."""
    return _trips(
        case,
        _current_generation_states(case).filter(method_version=METHOD_VERSION),
        "trip",
    )


def saved_trips(case, *, vehicle_id=None, start=None, end=None, kind="trip"):
    """Select complete saved trips overlapping a period, without cutting samples."""
    if kind not in ("trip", "gap"):
        raise ValidationError("Choose a supported trip selection.")
    for value in (start, end):
        if value is not None and timezone.is_naive(value):
            raise ValidationError("Trip times require an explicit timezone.")
    if start is not None and end is not None and start > end:
        raise ValidationError("The period end must not precede its start.")
    query = _trips(case, current_generations(case, vehicle_id=vehicle_id), kind)
    if start is not None:
        query = query.filter(end_at__gte=start)
    if end is not None:
        query = query.filter(start_at__lte=end)
    return query.order_by("-start_at", "pk")


def observation_trip_url(case, observation):
    """Link one current trip that contains this exact version of the observation."""
    if observation.case_id != case.pk or observation.is_archived:
        return ""
    membership = VehicleTripSource.objects.filter(
        case=case,
        trip_id=OuterRef("pk"),
        input__case=case,
        input__generation_id=OuterRef("generation_id"),
        input__observation=observation,
        input__sha256=observation.sha256,
    )
    trips = list(
        saved_trips(case, vehicle_id=observation.vehicle_id)
        .filter(Exists(membership))
        .values_list("pk", flat=True)[:2]
    )
    return (
        reverse("geo:trip_detail", args=(case.pk, trips[0])) if len(trips) == 1 else ""
    )


def trip_sources(case, trip):
    if not saved_trips(case, kind=trip.kind).filter(pk=trip.pk).exists():
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
    query = _trip_source_rows(case, trip)
    if query.count() != trip.source_count:
        raise Http404("Trip sources are unavailable.")
    return query


def _trip_source_rows(case, trip):
    return VehicleTripInput.objects.filter(
        case=case,
        generation_id=trip.generation_id,
        memberships__case=case,
        memberships__trip=trip,
        memberships__trip__case=case,
        observation__is_archived=False,
    ).order_by("memberships__ordinal")


def geometry_source_page(case, trip, cursor, size):
    """Revalidate only the requested contiguous slice of a current trip."""
    if trip.generation.method_version != METHOD_VERSION:
        return list(_source_rows(case, trip).filter(ordinal__gt=cursor)[:size])
    memberships = trip.sources.filter(case=case).order_by("ordinal")
    first = memberships.values_list("ordinal", flat=True).first()
    last = memberships.values_list("ordinal", flat=True).last()
    if first is None or last is None or last - first + 1 != trip.source_count:
        raise Http404("Trip sources are unavailable.")
    originals = scoped_evidence(case, "vehicle-location").filter(
        vehicle_id=trip.vehicle_id, is_archived=False
    )
    rows = [
        membership.input
        for membership in memberships.filter(
            ordinal__gt=cursor,
            input__case=case,
            input__generation_id=trip.generation_id,
            input__ordinal=F("ordinal"),
            input__observation__in=originals,
            input__observation__sha256=F("input__sha256"),
            input__snapshot__is_archived=False,
        ).select_related("input")[:size]
    ]
    start = max(first, cursor + 1)
    if [row.ordinal for row in rows] != list(range(start, min(start + size, last + 1))):
        raise Http404("Trip sources are unavailable.")
    return rows


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
    return _format_trip(case, trip, first, last, listed)


def format_trips(case, trips):
    """Project both endpoints for one page of already scoped trips."""
    trips = list(trips)
    # Only fetch retained snapshots after narrowing to the endpoint IDs.
    edges = list(
        VehicleTripSource.objects.filter(
            case=case,
            trip_id__in=[trip.pk for trip in trips],
            trip__case=case,
            input__case=case,
            input__generation_id=F("trip__generation_id"),
            input__observation__is_archived=False,
        )
        .annotate(
            total=Window(Count("pk"), partition_by="trip_id"),
            first=Window(Min("ordinal"), partition_by="trip_id"),
            last=Window(Max("ordinal"), partition_by="trip_id"),
        )
        .filter(Q(ordinal=F("first")) | Q(ordinal=F("last")))
        .order_by("trip_id", "ordinal")
        .values_list("trip_id", "input_id", "total")
    )
    inputs = VehicleTripInput.objects.filter(case=case).in_bulk(
        [input_id for _, input_id, _ in edges]
    )
    by_trip = defaultdict(list)
    for trip_id, input_id, count in edges:
        by_trip[trip_id].append((inputs.get(input_id), count))
    result = []
    for trip in trips:
        endpoints = by_trip[trip.pk]
        if not endpoints:
            raise Http404("Trip sources are unavailable.")
        first, count = endpoints[0]
        last, _ = endpoints[-1]
        if first is None or last is None or count != trip.source_count:
            raise Http404("Trip sources are unavailable.")
        result.append(_format_trip(case, trip, first, last))
    return result


def _format_trip(case, trip, first, last, listed=()):
    start, end = format_source(case, first), format_source(case, last)
    trip_url = reverse("geo:trip_detail", args=(case.pk, trip.pk))
    return {
        "id": str(trip.pk),
        "generation_id": str(trip.generation_id),
        "label": "Possible trip" if trip.kind == "trip" else "Coverage gap",
        "text": "Movement suggested by the retained observations.",
        "url": trip_url,
        "trip_url": trip_url,
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

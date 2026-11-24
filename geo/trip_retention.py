"""Bounded cleanup of obsolete derived rows that no analysis references."""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import CharField, Exists, F, OuterRef, Q, Value
from django.db.models.functions import Cast, JSONArray, JSONObject
from django.http import Http404

from core.models import AnalysisRun, DeletionReview
from geo.analysis.deterministic.trips import CHUNK_SIZE
from geo.trip_jobs import _authorize, _database_limits
from geo.trip_models import (
    VehicleTrip,
    VehicleTripGeneration,
    VehicleTripInput,
    VehicleTripSource,
    VehicleTripState,
)


def obsolete_generations_with_snapshots(current):
    """Find finished, replaced generations that can have disposable rows."""
    inputs = VehicleTripInput.objects.filter(generation_id=OuterRef("pk"))
    trips = VehicleTrip.objects.filter(generation_id=OuterRef("pk"))
    return (
        VehicleTripGeneration.objects.filter(
            case_id=current.case_id,
            state_id=current.state_id,
            vehicle_id=current.vehicle_id,
            revision__lte=current.revision,
            status__in=("complete", "failed", "superseded"),
        )
        .exclude(pk=current.pk)
        .filter(Exists(inputs) | Exists(trips))
    )


@transaction.atomic
def retire_snapshots(generation_id):
    """Keep generation history and protect every saved or queued trip citation."""
    generation = (
        VehicleTripGeneration.objects.select_related("vehicle", "state")
        .filter(pk=generation_id, status="complete")
        .first()
    )
    if generation is None:
        return "unavailable"
    _database_limits()
    try:
        case = _authorize(generation, lock=True)
    except PermissionDenied, Http404:
        return "access_removed"
    if (
        not VehicleTripState.objects.select_for_update()
        .filter(
            pk=generation.state_id,
            case=case,
            vehicle__case=case,
            revision=generation.revision,
            current_generation=generation,
        )
        .exists()
    ):
        return "unavailable"
    references = AnalysisRun.objects.filter(
        case=case,
        operation="journey_claims",
        parameters__groups__contains=JSONArray(
            JSONObject(
                origin=Value("trip"), generation_id=Cast(OuterRef("pk"), CharField())
            )
        ),
    )
    inconsistent = VehicleTripSource.objects.filter(
        Q(input__generation_id=OuterRef("pk")) | Q(trip__generation_id=OuterRef("pk"))
    ).exclude(
        case=case,
        input__case=case,
        trip__case=case,
        input__generation_id=F("trip__generation_id"),
    )
    inputs = VehicleTripInput.objects.filter(generation_id=OuterRef("pk"))
    trips = VehicleTrip.objects.filter(generation_id=OuterRef("pk"))
    stale = (
        obsolete_generations_with_snapshots(generation)
        .filter(
            ~Exists(references),
            ~Exists(inconsistent),
            ~Exists(inputs.exclude(case=case)),
            ~Exists(trips.exclude(case=case, vehicle_id=OuterRef("vehicle_id"))),
        )
        .first()
    )
    if stale is None:
        return "complete"
    inputs = stale.inputs.filter(case=case)
    ids = list(inputs.order_by("pk").values_list("pk", flat=True)[:CHUNK_SIZE])
    if ids:
        inputs.filter(pk__in=ids).delete()
    else:
        trips = stale.trips.filter(case=case)
        ids = list(trips.order_by("pk").values_list("pk", flat=True)[:CHUNK_SIZE])
        if not ids:
            return "unavailable"
        trips.filter(pk__in=ids).delete()
    DeletionReview.objects.filter(case=case).delete()
    from geo.tasks import retire_trip_snapshots

    retire_trip_snapshots.enqueue(str(generation_id))
    return "queued"

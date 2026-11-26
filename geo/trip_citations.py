"""Bounded, source-verifiable references to saved vehicle trips."""

from django.db.models import Exists, F, OuterRef, Q

from core.evidence.queries import scoped_evidence
from core.models import ChatSource
from geo.analysis.deterministic.trips import _snapshot
from geo.trip_processing import METHOD_VERSION, _published_generation_states, _trips

# A chat citation carries every observation identity used in the saved trip.
# Larger trips remain available in the paginated Trips workspace.
MAX_OBSERVATIONS = 64


def citation_trips(case, *, retained=False):
    generations = _published_generation_states(case)
    trips = _trips(case, generations, "trip").filter(
        source_count__range=(2, MAX_OBSERVATIONS)
    )
    current = Q(
        generation__state__current_generation_id=F("generation_id"),
        generation__state__revision=F("generation__revision"),
    )
    if retained:
        pins = ChatSource.objects.filter(
            case=case,
            turn__case=case,
            turn__chat__case=case,
            evidence_kind="possible-trip",
            evidence_id=OuterRef("pk"),
        )
        current |= Q(Exists(pins))
    return trips.filter(current)


def citation_inputs(case, trip):
    """Validate the exact ordered membership, original hashes and saved endpoints."""
    if not 2 <= trip.source_count <= MAX_OBSERVATIONS:
        return []
    # Reject any extra, potentially out-of-scope membership before reading
    # private fields. The offset stays within the observation ceiling.
    if trip.sources.order_by("ordinal")[
        trip.source_count : trip.source_count + 1
    ].exists():
        return []
    originals = (
        scoped_evidence(case, "vehicle-location")
        .filter(vehicle_id=trip.vehicle_id, is_archived=False)
        .exclude(import_batch__document__is_archived=True)
    )
    members = list(
        trip.sources.filter(
            case=case,
            input__case=case,
            input__generation_id=trip.generation_id,
            input__observation__in=originals,
            input__sha256=F("input__observation__sha256"),
        )
        .order_by("ordinal")
        .select_related("input__observation")
        .defer(
            "input__observation__raw_row",
            "input__observation__description",
            "input__observation__source_description",
        )[: MAX_OBSERVATIONS + 1]
    )
    inputs = [member.input for member in members]
    if (
        len(inputs) != trip.source_count
        or len({item.observation_id for item in inputs}) != trip.source_count
        or inputs[0].observation.observed_at != trip.start_at
        or inputs[-1].observation.observed_at != trip.end_at
        or any(item.snapshot != _snapshot(item.observation) for item in inputs)
    ):
        return []
    if trip.generation.method_version == METHOD_VERSION and (
        members[-1].ordinal - members[0].ordinal + 1 != trip.source_count
        or any(member.ordinal != member.input.ordinal for member in members)
    ):
        return []
    return inputs


def citation_fields(trip, inputs):
    first, last = inputs[0].snapshot, inputs[-1].snapshot

    def position(snapshot):
        return snapshot["address"] or f"{snapshot['latitude']}, {snapshot['longitude']}"

    return [
        ("First observation (UTC)", trip.start_at.isoformat()),
        ("First recorded place", position(first)),
        ("Last observation (UTC)", trip.end_at.isoformat()),
        ("Last recorded place", position(last)),
        ("Reported vehicle", str(trip.vehicle)),
        ("Observations", trip.source_count),
        ("Basis", trip.basis),
        (
            "Interpretation",
            "Observations suggest movement. The route and driver are unknown.",
        ),
    ]

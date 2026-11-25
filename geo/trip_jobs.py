"""Bounded, resumable trip segmentation and local address preparation."""

from datetime import datetime
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import F, Q
from django.http import Http404
from django.utils import timezone

from cases.permissions import get_case
from core.evidence.queries import scoped_evidence
from geo.analysis.deterministic.trips import CHUNK_SIZE, Segmenter, _snapshot
from geo.trip_models import (
    VehicleTripGeneration,
    VehicleTripInput,
    VehicleTripState,
)
from geo.trip_processing import (
    METHOD_VERSION,
    READABLE_METHODS,
    _queue_geocoding,
    state_generations,
)


class SupersededTrip(Exception):
    pass


def _authorize(generation, *, lock=False, geocode=False):
    actor_id = (
        generation.geocode_requested_by_id if geocode else generation.requested_by_id
    )
    try:
        actor = get_user_model().objects.get(pk=actor_id)
    except get_user_model().DoesNotExist:
        raise Http404 from None
    case = get_case(actor, generation.case_id, edit=True, lock=lock)
    if not EmailAddress.objects.filter(user=actor, verified=True).exists():
        raise PermissionDenied
    if (
        generation.vehicle.case_id != case.pk
        or generation.state.case_id != case.pk
        or generation.state.vehicle_id != generation.vehicle_id
    ):
        raise Http404
    return case


def _database_limits():
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '30s'")
        cursor.execute("SET LOCAL lock_timeout = '10s'")


def _current(generation, *, geocode=False):
    state = VehicleTripState.objects.select_for_update().get(
        pk=generation.state_id,
        case_id=generation.case_id,
        vehicle_id=generation.vehicle_id,
    )
    current = (
        state_generations(state).select_for_update(of=("self",)).get(pk=generation.pk)
    )
    expected = (
        current.status == "complete"
        and current.geocode_status == "running"
        and state.current_generation_id == current.pk
        if geocode
        else current.status == "running"
    )
    if (
        current.token != generation.token
        or state.revision != current.revision
        or not expected
    ):
        raise SupersededTrip
    if current.method_version not in (
        READABLE_METHODS if geocode else (METHOD_VERSION,)
    ):
        raise ValueError("version_unavailable")
    return state, current


def _next_sources(generation, case):
    queryset = scoped_evidence(case, "vehicle-location").filter(
        vehicle_id=generation.vehicle_id, is_archived=False
    )
    cursor = generation.cursor
    phase = cursor.get("phase", "timed")
    if phase == "timed":
        queryset = queryset.filter(observed_at__isnull=False)
        if cursor.get("time"):
            instant = datetime.fromisoformat(cursor["time"])
            queryset = queryset.filter(
                Q(observed_at__gt=instant) | Q(observed_at=instant, pk__gt=cursor["id"])
            )
        rows = list(queryset.order_by("observed_at", "pk")[:CHUNK_SIZE])
        if len(rows) < CHUNK_SIZE:
            generation.cursor = {"phase": "untimed"}
        elif rows:
            generation.cursor = {
                "phase": "timed",
                "time": rows[-1].observed_at.isoformat(),
                "id": str(rows[-1].pk),
            }
        return rows, False
    queryset = queryset.filter(observed_at__isnull=True)
    if cursor.get("id"):
        queryset = queryset.filter(pk__gt=cursor["id"])
    rows = list(queryset.order_by("pk")[:CHUNK_SIZE])
    if rows:
        generation.cursor = {"phase": "untimed", "id": str(rows[-1].pk)}
    return rows, len(rows) < CHUNK_SIZE


def _claim(generation_id, token, *, geocode=False):
    with transaction.atomic():
        generation = (
            VehicleTripGeneration.objects.select_for_update(of=("self",))
            .select_related("requested_by", "geocode_requested_by", "vehicle", "state")
            .filter(pk=generation_id)
            .first()
        )
        if generation is None or str(generation.token) != str(token):
            return None
        if geocode:
            if generation.status != "complete" or generation.geocode_status != "queued":
                return None
            generation.geocode_status = "running"
            generation.save(update_fields=("geocode_status",))
        else:
            if generation.status != "queued":
                return None
            generation.status = "running"
            generation.started_at = generation.started_at or timezone.now()
            generation.save(update_fields=("status", "started_at"))
        return generation


def process_trips(generation_id, token):
    generation = _claim(generation_id, token)
    if generation is None:
        return "unavailable"
    try:
        with transaction.atomic():
            _database_limits()
            case = _authorize(generation, lock=True)
            state, current = _current(generation)
            segmenter = Segmenter(current)
            segmenter.validate_carry()
            rows, complete = _next_sources(current, case)
            inputs = [
                VehicleTripInput(
                    case=case,
                    generation=current,
                    observation=item,
                    ordinal=current.processed_sources + index + 1,
                    sha256=item.sha256,
                    snapshot=_snapshot(item),
                )
                for index, item in enumerate(rows)
            ]
            VehicleTripInput.objects.bulk_create(inputs, batch_size=CHUNK_SIZE)
            for source in inputs:
                snapshot = source.snapshot | {
                    "input_id": str(source.pk),
                    "ordinal": source.ordinal,
                }
                segmenter.process(snapshot)
            current.processed_sources += len(inputs)
            if complete:
                segmenter.complete()
                if current.processed_sources != current.total_sources:
                    raise SupersededTrip
                _authorize(generation)
                current.status = "complete"
                current.finished_at = timezone.now()
                state.current_generation = current
                state.save(update_fields=("current_generation",))
                _queue_geocoding(generation.requested_by, current)
                from geo.trip_retention import obsolete_generations_with_snapshots

                if obsolete_generations_with_snapshots(current).exists():
                    from geo.tasks import retire_trip_snapshots

                    retire_trip_snapshots.enqueue(str(current.pk))
            else:
                segmenter.flush()
                current.status = "queued"
                current.token = uuid4()
                from geo.tasks import process_vehicle_trips

                process_vehicle_trips.enqueue(str(current.pk), str(current.token))
            current.save()
            return current.status
    except SupersededTrip:
        status, code = "superseded", ""
    except PermissionDenied, Http404:
        status, code = "failed", "access_removed"
    except ValueError as error:
        status, code = (
            "failed",
            "version_unavailable"
            if str(error) == "version_unavailable"
            else "processing_failed",
        )
    except Exception:
        status, code = "failed", "processing_failed"
    VehicleTripGeneration.objects.filter(
        pk=generation.pk, token=generation.token, status="running"
    ).update(status=status, error_code=code, finished_at=timezone.now())
    return status


def prepare_addresses(generation_id, token):
    """Prepare bounded existing geocode runs; trip publication never waits for them."""
    generation = _claim(generation_id, token, geocode=True)
    if generation is None:
        return "unavailable"
    try:
        with transaction.atomic():
            _database_limits()
            case = _authorize(generation, lock=True, geocode=True)
            _, current = _current(generation, geocode=True)
            from geo.map_geocoding import (
                address_estimates,
                local_geocoder_ready,
                queue_addresses,
            )

            if not local_geocoder_ready():
                current.geocode_status = "disabled"
                current.save(update_fields=("geocode_status",))
                return "disabled"
            query = (
                current.inputs.filter(
                    case=case,
                    ordinal__gt=current.geocode_cursor,
                    memberships__case=case,
                    memberships__ordinal=F("ordinal"),
                    memberships__trip__case=case,
                    memberships__trip__vehicle_id=current.vehicle_id,
                    memberships__trip__generation=current,
                    memberships__trip__kind="trip",
                    memberships__trip__evidence_state="active",
                )
                .distinct()
                .order_by("ordinal")
            )
            source_ids = list(
                query.values_list("pk", flat=True)[
                    : min(CHUNK_SIZE, settings.PANDECT_ANALYSIS_MAX_SOURCES)
                ]
            )
            originals = scoped_evidence(case, "vehicle-location").filter(
                vehicle_id=current.vehicle_id, is_archived=False
            )
            sources = list(
                current.inputs.filter(
                    case=case,
                    pk__in=source_ids,
                    observation__in=originals,
                    sha256=F("observation__sha256"),
                )
                .select_related("observation")
                .order_by("ordinal")
            )
            if len(sources) != len(source_ids):
                raise ValueError("invalid_trip_sources")
            rows, addresses, handled = [], set(), []
            for source in sources:
                value = _snapshot(source.observation)
                if (
                    value["latitude"] is None
                    and value["address"].strip()
                    and len(value["address"]) <= 2000
                ):
                    if value["address"] not in addresses and len(addresses) >= 25:
                        break
                    addresses.add(value["address"])
                handled.append(source)
                rows.append(
                    {
                        "id": source.observation_id,
                        "kind": "vehicle-location",
                        "number": source.ordinal,
                        "title": value["title"],
                        "address": value["address"],
                        "latitude": value["latitude"],
                        "longitude": value["longitude"],
                        "sha256": source.sha256,
                        "url": "",
                    }
                )
            address_estimates(case, rows)
            queue_addresses(current.geocode_requested_by, case, rows)
            if handled:
                current.geocode_cursor = handled[-1].ordinal
            more = query.filter(ordinal__gt=current.geocode_cursor).exists()
            current.geocode_status = "queued" if more else "complete"
            if more:
                current.token = uuid4()
                from geo.tasks import prepare_trip_addresses

                prepare_trip_addresses.enqueue(str(current.pk), str(current.token))
            current.save(update_fields=("geocode_cursor", "geocode_status", "token"))
            return current.geocode_status
    except SupersededTrip:
        status, code = "superseded", ""
    except Http404, PermissionDenied:
        status, code = "failed", "access_removed"
    except Exception:
        status, code = "failed", "preparation_failed"
    VehicleTripGeneration.objects.filter(
        pk=generation.pk, token=generation.token
    ).update(geocode_status=status, geocode_error_code=code)
    return status

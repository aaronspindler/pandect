"""Deterministic full-history trip segmentation and saved report entry point."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.db.models import Count, F, Max

from core.evidence.queries import scoped_evidence
from geo.analysis.deterministic.locations import normalize_address
from geo.coordinates import distance_m
from geo.trip_models import VehicleTrip, VehicleTripSource

CHUNK_SIZE = 500
GAP_SECONDS = 3600
STOP_SECONDS = 300
STOP_RADIUS_M = 100


def _snapshot(item):
    return {
        "id": str(item.pk),
        "title": item.title,
        "address": item.address,
        "normalized_address": normalize_address(item.address)["comparison_key"],
        "latitude": str(item.latitude) if item.latitude is not None else None,
        "longitude": str(item.longitude) if item.longitude is not None else None,
        "observed_at": item.observed_at.isoformat() if item.observed_at else None,
        "speed_kmh": str(item.speed_kmh) if item.speed_kmh is not None else None,
        "is_archived": item.is_archived,
        "source_row": item.source_row,
        "import_id": str(item.import_batch_id) if item.import_batch_id else None,
    }


def observed_time(source):
    return datetime.fromisoformat(source["observed_at"])


def _same(first, second):
    if first["latitude"] is not None and second["latitude"] is not None:
        return (
            distance_m(
                (first["latitude"], first["longitude"]),
                (second["latitude"], second["longitude"]),
            )
            <= STOP_RADIUS_M
        )
    if first["normalized_address"] and second["normalized_address"]:
        return first["normalized_address"] == second["normalized_address"]
    return None


def _positive(source):
    return source["speed_kmh"] is not None and Decimal(source["speed_kmh"]) > 0


class Segmenter:
    """Carry a few boundary samples while ordered memberships remain in PostgreSQL."""

    def __init__(self, generation):
        self.generation = generation
        self.pending = []
        self.trips = generation.trips.filter(
            case_id=generation.case_id, vehicle_id=generation.vehicle_id
        )

    def validate_carry(self):
        """Verify the persisted boundary samples before resuming an open trip."""
        carry = self.generation.carry
        if not carry:
            return
        trip = self.trips.get(pk=UUID(carry["trip_id"]), kind="trip")
        originals = scoped_evidence(self.generation.case_id, "vehicle-location").filter(
            vehicle_id=self.generation.vehicle_id, is_archived=False
        )
        sources = [carry[key] for key in ("previous", "anchor", "run_start", "origin")]
        source_ids = {UUID(source["input_id"]) for source in sources}
        inputs = {
            str(row.pk): row.snapshot
            | {"input_id": str(row.pk), "ordinal": row.ordinal}
            for row in self.generation.inputs.filter(
                case_id=self.generation.case_id,
                pk__in=source_ids,
                observation__in=originals,
                sha256=F("observation__sha256"),
            )
        }
        if any(inputs.get(source["input_id"]) != source for source in sources):
            raise ValueError("invalid_trip_checkpoint")
        if trip.sources.exclude(
            case_id=self.generation.case_id,
            input__case_id=self.generation.case_id,
            input__generation_id=self.generation.pk,
            input__observation__case_id=self.generation.case_id,
            input__observation__vehicle_id=self.generation.vehicle_id,
            input__ordinal=F("ordinal"),
        ).exists():
            raise ValueError("invalid_trip_checkpoint")

    def flush(self):
        if self.pending:
            VehicleTripSource.objects.bulk_create(self.pending, batch_size=CHUNK_SIZE)
            self.pending = []

    def create(self, entry, source, *, kind="trip"):
        ordinal = entry.get("next_ordinal", 1)
        entry["next_ordinal"] = ordinal + 1
        return VehicleTrip.objects.create(
            case_id=self.generation.case_id,
            vehicle_id=self.generation.vehicle_id,
            generation=self.generation,
            evidence_state="active",
            kind=kind,
            ordinal=ordinal,
            start_at=observed_time(source),
            end_at=observed_time(source),
        )

    def append(self, trip_id, source, seconds=0):
        self.pending.append(
            VehicleTripSource(
                case_id=self.generation.case_id,
                trip_id=trip_id,
                input_id=source["input_id"],
                ordinal=source["ordinal"],
                gap_seconds=seconds,
            )
        )

    def candidate(self, entry, source):
        trip = self.create(entry, source)
        entry.update(trip_id=str(trip.pk), origin=source, first_movement=None)
        self.append(trip.pk, source)

    def finish(self, entry, end):
        trip_id = entry.get("trip_id")
        if not trip_id:
            return
        self.flush()
        origin = entry["origin"]
        moved = entry.get("first_movement")
        trip = self.trips.get(pk=trip_id, kind="trip")
        if (
            moved is None
            or moved > end["ordinal"]
            or observed_time(end) <= observed_time(origin)
        ):
            trip.delete()
        else:
            trip.sources.filter(ordinal__gt=end["ordinal"]).delete()
            totals = trip.sources.aggregate(
                count=Count("pk"), largest=Max("gap_seconds")
            )
            trip.end_at = observed_time(end)
            trip.source_count = totals["count"]
            trip.largest_gap_seconds = totals["largest"] or 0
            trip.basis = (
                "Reported addresses and available coordinates"
                if trip.sources.filter(input__snapshot__latitude=None).exists()
                else "Reported coordinates"
            )
            trip.save(
                update_fields=("end_at", "source_count", "largest_gap_seconds", "basis")
            )
            self.generation.metrics["active"]["trips"] += 1
        entry["trip_id"] = None

    def reset(self, entry, source):
        entry.update(
            previous=source,
            anchor=source,
            run_start=source,
            run_positive=_positive(source),
        )
        self.candidate(entry, source)

    def stopped(self, entry):
        return (
            not entry["run_positive"]
            and (
                observed_time(entry["previous"]) - observed_time(entry["run_start"])
            ).total_seconds()
            >= STOP_SECONDS
        )

    def finish_block(self, entry):
        self.finish(
            entry,
            entry["run_start"] if self.stopped(entry) else entry["previous"],
        )

    def gap(self, entry, first, last, seconds):
        trip = self.create(entry, first, kind="gap")
        trip.end_at = observed_time(last)
        trip.source_count = 2
        trip.largest_gap_seconds = seconds
        trip.basis = "Reported observation times"
        trip.save(
            update_fields=("end_at", "source_count", "largest_gap_seconds", "basis")
        )
        self.append(trip.pk, first)
        self.append(trip.pk, last, seconds)
        self.generation.metrics["active"]["gaps"] += 1

    def process(self, source):
        metrics = self.generation.metrics["active"]
        metrics["observations"] += 1
        if source["observed_at"] is None:
            metrics["missing_times"] += 1
            return
        entry = self.generation.carry
        previous = entry.get("previous")
        if previous is None:
            self.reset(entry, source)
            return
        seconds = (observed_time(source) - observed_time(previous)).total_seconds()
        same = _same(previous, source)
        if seconds > GAP_SECONDS or same is None or (seconds == 0 and same is False):
            self.finish_block(entry)
            if seconds > GAP_SECONDS:
                self.gap(entry, previous, source, seconds)
            else:
                metrics["incomparable"] += same is None
                metrics["conflicts"] += seconds == 0 and same is False
            self.reset(entry, source)
            return
        same_run = _same(entry["anchor"], source) is True
        if not same_run and self.stopped(entry):
            self.finish(entry, entry["run_start"])
            self.candidate(entry, previous)
        self.append(entry["trip_id"], source, seconds)
        if entry["first_movement"] is None and _same(entry["origin"], source) is False:
            entry["first_movement"] = source["ordinal"]
        if same_run:
            entry["run_positive"] |= _positive(source)
        else:
            entry.update(
                anchor=source, run_start=source, run_positive=_positive(source)
            )
        entry["previous"] = source

    def complete(self):
        if self.generation.carry.get("previous"):
            self.finish_block(self.generation.carry)
        self.flush()
        self.generation.carry = {}


def vehicle_trip_report(case, vehicle_id, start, end, limit, page=1, gap_page=1):
    """Page complete saved trips; observation history is never sliced here."""
    from geo.trip_reports import saved_trip_report

    return saved_trip_report(
        case,
        vehicle_id=vehicle_id,
        start=start,
        end=end,
        page=page,
        page_size=min(limit, 25),
        gap_page=gap_page,
    )

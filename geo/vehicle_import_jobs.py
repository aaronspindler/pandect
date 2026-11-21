"""Case-authorized, streaming CSV preparation and atomic publication."""

import time
from itertools import batched
from uuid import uuid4, uuid5

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from cases.permissions import get_case
from geo.models import (
    VehicleLocationEvidence,
    VehicleLocationImport,
    VehicleLocationImportRow,
)
from geo.vehicle_imports import MAX_GROUPS, SUPPORTED_PARSER_VERSIONS, iter_import
from vehicle.models import Vehicle

CHUNK_SIZE = 500
MAX_JOB_SECONDS = 600
ERROR_MESSAGES = {
    "processing_failed": (
        "Processing stopped. No observations were imported. "
        "Retry after checking the worker."
    ),
    "source_unavailable": (
        "The retained CSV could not be read. Restore its original file and retry."
    ),
    "source_invalid": (
        "The CSV could not be verified or interpreted. Download the original "
        "to check its format and upload a corrected CSV."
    ),
    "access_removed": (
        "The requester needs edit access and a verified email address. "
        "A verified owner or editor can retry this import."
    ),
    "vehicle_unavailable": (
        "A selected vehicle is no longer available in this case. "
        "Review the vehicle choices."
    ),
    "version_unavailable": (
        "This import's parser version is unavailable. "
        "Restore the matching worker version or upload a new CSV."
    ),
    "limit_exceeded": (
        "Processing exceeded its time or asset limit. "
        "Split the CSV into smaller files and upload them separately."
    ),
}


class ImportJobError(Exception):
    def __init__(self, code):
        self.code = code


class SupersededImport(Exception):
    pass


def _locked_batch(actor, case_id, import_id):
    case = get_case(actor, case_id, edit=True, lock=True)
    batch = get_object_or_404(
        VehicleLocationImport.objects.select_for_update(of=("self",)).select_related(
            "document", "requested_by"
        ),
        pk=import_id,
        case=case,
        document__case=case,
    )
    return case, batch


def _enqueue(batch, actor):
    if batch.attempts >= 3:
        raise ValidationError(
            "This processing step has reached its three-attempt limit. "
            "Upload a new CSV after correcting the problem."
        )
    batch.status = "queued"
    batch.requested_by = actor
    batch.job_token = uuid4()
    batch.attempts += 1
    batch.processed_rows = 0
    batch.started_at = None
    batch.finished_at = None
    batch.error_code = ""
    batch.save()
    from geo.tasks import process_vehicle_import

    # Both writes share the same PostgreSQL transaction, including upload callers.
    process_vehicle_import.enqueue(str(batch.pk), str(batch.job_token))
    return batch


@transaction.atomic
def enqueue_preparation(actor, case_id, import_id):
    _, batch = _locked_batch(actor, case_id, import_id)
    if batch.completed_at or batch.status != "pending":
        return batch
    batch.phase = "prepare"
    return _enqueue(batch, actor)


def _validate_mapping(batch, vehicle_mapping):
    if not isinstance(vehicle_mapping, dict) or set(vehicle_mapping) != {
        str(index) for index in range(len(batch.groups))
    }:
        raise ValidationError("Choose a case vehicle for every asset in this CSV.")
    try:
        identifiers = {str(value) for value in vehicle_mapping.values()}
        vehicles = {
            str(vehicle.pk): vehicle
            for vehicle in Vehicle.objects.filter(
                case_id=batch.case_id, pk__in=identifiers
            )
        }
    except ValueError, ValidationError:
        raise ValidationError("Choose vehicles in this case.") from None
    if identifiers != set(vehicles):
        raise ValidationError("Choose vehicles in this case.")
    return vehicles


@transaction.atomic
def enqueue_completion(
    actor, case_id, import_id, vehicle_mapping, confirm_duplicates=False
):
    _, batch = _locked_batch(actor, case_id, import_id)
    if batch.completed_at:
        return batch
    _validate_mapping(batch, vehicle_mapping)
    if batch.phase == "publish" and batch.status in {"queued", "running"}:
        if batch.vehicle_mapping != vehicle_mapping:
            raise ValidationError(
                "This import is already queued with different vehicle choices."
            )
        return batch
    if batch.status != "review" or batch.invalid_count or not batch.row_count:
        raise ValidationError(
            "Wait for a valid CSV review before importing observations."
        )
    batch.vehicle_mapping = vehicle_mapping
    batch.duplicates_confirmed = bool(confirm_duplicates)
    batch.phase = "publish"
    batch.attempts = 0
    return _enqueue(batch, actor)


@transaction.atomic
def retry_import(actor, case_id, import_id, worker_stopped=False):
    _, batch = _locked_batch(actor, case_id, import_id)
    if batch.completed_at:
        return batch
    if batch.status == "running" and not worker_stopped:
        raise ValidationError(
            "Confirm the original worker has stopped before recovering running work."
        )
    if batch.status not in {"failed", "running"}:
        raise ValidationError("Only failed or abandoned running work can be retried.")
    if batch.phase == "publish":
        _validate_mapping(batch, batch.vehicle_mapping)
    return _enqueue(batch, actor)


def _authorize(batch, *, lock=False):
    try:
        actor = get_user_model().objects.get(pk=batch.requested_by_id)
    except get_user_model().DoesNotExist:
        raise Http404 from None
    case = get_case(actor, batch.case_id, edit=True, lock=lock)
    if not EmailAddress.objects.filter(user=actor, verified=True).exists():
        raise PermissionDenied("Verify your email before processing.")
    return case


def _current(batch):
    current = VehicleLocationImport.objects.select_for_update().get(pk=batch.pk)
    if current.job_token != batch.job_token or current.status != "running":
        raise SupersededImport
    return current


def _check_time(started):
    if time.monotonic() - started > MAX_JOB_SECONDS:
        raise ImportJobError("limit_exceeded")


def _database_limits():
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '60s'")
        cursor.execute("SET LOCAL lock_timeout = '10s'")


def _existing_fingerprints(batch, fingerprints):
    return set(
        VehicleLocationEvidence.objects.filter(
            case_id=batch.case_id,
            vehicle__case_id=batch.case_id,
            import_batch__case_id=batch.case_id,
            import_batch__document__case_id=batch.case_id,
            import_batch__timezone=batch.timezone,
            raw_fingerprint__in=fingerprints,
        )
        .order_by()
        .values_list("raw_fingerprint", flat=True)
        .distinct()
    )


def _duplicate_flags(batch, fingerprints, seen):
    existing = _existing_fingerprints(batch, set(fingerprints) - seen)
    for fingerprint in fingerprints:
        duplicate = fingerprint in seen or fingerprint in existing
        seen.add(fingerprint)
        yield duplicate


def _stage_chunk(batch, rows, groups, counts, seen, started):
    _check_time(started)
    with transaction.atomic():
        _database_limits()
        _authorize(batch, lock=True)
        current = _current(batch)
        fingerprints = [row.fingerprint for row in rows]
        staged = []
        duplicates = _duplicate_flags(batch, fingerprints, seen)
        for row, fingerprint, duplicate in zip(
            rows, fingerprints, duplicates, strict=True
        ):
            counts["invalid_count"] += bool(row.errors)
            counts["duplicate_count"] += duplicate
            # Invalid normalized values may exceed database bounds; originals and
            # errors remain reviewable without inserting them into typed columns.
            values = row.values if not row.errors else {}
            staged.append(
                VehicleLocationImportRow(
                    batch=batch,
                    number=row.number,
                    group_index=groups.index(list(row.group)),
                    raw=row.raw,
                    errors=row.errors,
                    duplicate=duplicate,
                    fingerprint=fingerprint,
                    observed_at=values.get("observed_at"),
                    received_at=values.get("received_at"),
                    speed_kmh=values.get("speed_kmh"),
                )
            )
        VehicleLocationImportRow.objects.bulk_create(staged, batch_size=CHUNK_SIZE)
        current.groups = groups
        current.processed_rows += len(staged)
        current.invalid_count = counts["invalid_count"]
        current.duplicate_count = counts["duplicate_count"]
        current.save(
            update_fields=(
                "groups",
                "processed_rows",
                "invalid_count",
                "duplicate_count",
            )
        )


def _prepare(batch, started):
    with transaction.atomic():
        _database_limits()
        _authorize(batch, lock=True)
        current = _current(batch)
        current.rows.all().delete()
        current.groups = []
        current.row_count = current.processed_rows = current.invalid_count = (
            current.duplicate_count
        ) = 0
        current.save()
    rows, groups = [], []
    counts = {"invalid_count": 0, "duplicate_count": 0}
    seen = set()
    for row in iter_import(batch):
        group = list(row.group)
        if group not in groups:
            if len(groups) >= MAX_GROUPS:
                raise ImportJobError("limit_exceeded")
            groups.append(group)
        rows.append(row)
        if len(rows) == CHUNK_SIZE:
            _stage_chunk(batch, rows, groups, counts, seen, started)
            rows = []
    if rows:
        _stage_chunk(batch, rows, groups, counts, seen, started)
    with transaction.atomic():
        _database_limits()
        _authorize(batch, lock=True)
        current = _current(batch)
        current.row_count = current.processed_rows
        current.status = "review"
        current.finished_at = timezone.now()
        current.save()


def _refresh_duplicates(batch, started):
    seen = set()
    count = 0
    staged = (
        batch.rows.order_by("number")
        .only("fingerprint", "duplicate")
        .iterator(chunk_size=CHUNK_SIZE)
    )
    for rows in batched(staged, CHUNK_SIZE, strict=False):
        _check_time(started)
        flags = _duplicate_flags(batch, [row.fingerprint for row in rows], seen)
        changed = []
        for row, duplicate in zip(rows, flags, strict=True):
            count += duplicate
            if row.duplicate != duplicate:
                row.duplicate = duplicate
                changed.append(row)
        VehicleLocationImportRow.objects.bulk_update(changed, ("duplicate",))
    return count


def _publish(batch, started):
    # Re-read and verify the retained original before publishing its staged rows.
    for index, _ in enumerate(iter_import(batch)):
        if index % CHUNK_SIZE == 0:
            _check_time(started)
            _authorize(batch)
    with transaction.atomic():
        _database_limits()
        case = _authorize(batch, lock=True)
        current = _current(batch)
        # Keep each PostgreSQL operation bounded independently of the job deadline.
        try:
            vehicles = _validate_mapping(current, current.vehicle_mapping)
        except ValidationError:
            raise ImportJobError("vehicle_unavailable") from None
        if current.invalid_count or current.rows.count() != current.row_count:
            raise ImportJobError("source_invalid")
        # Observations may have been imported or deleted since the preview.
        current.duplicate_count = _refresh_duplicates(current, started)
        import_rows = current.rows.all()
        if not current.duplicates_confirmed:
            import_rows = import_rows.filter(duplicate=False)
        for rows in batched(
            import_rows.iterator(chunk_size=CHUNK_SIZE), CHUNK_SIZE, strict=False
        ):
            _check_time(started)
            _authorize(batch)
            items = []
            for row in rows:
                observed_label = row.observed_at.strftime("%d %b %Y, %H:%M:%S UTC")
                asset_label = (row.asset or row.serial)[:180]
                item = VehicleLocationEvidence(
                    case=case,
                    uploaded_by_id=current.requested_by_id,
                    title=f"{asset_label} · {observed_label}",
                    source=batch.document.source,
                    vehicle=vehicles[current.vehicle_mapping[str(row.group_index)]],
                    import_batch=batch,
                    source_row=row.number,
                    raw_row=row.raw,
                    **row.values,
                )
                # The parser validates every row and shared references were checked
                # once above. Preserve original normalization/checksums without
                # per-row foreign-key queries and redundant model saves.
                item._prepare_original()
                items.append(item)
            VehicleLocationEvidence.objects.bulk_create(items, batch_size=CHUNK_SIZE)
        _authorize(batch)
        current.status = "complete"
        current.completed_at = current.finished_at = timezone.now()
        current.processed_rows = current.row_count
        current.save()
        from core.analysis.bulk import start_case_analysis
        from core.analysis.reprocessing import analysis_scope
        from geo.trip_processing import invalidate_vehicle_trips

        if current.observations.exists():
            start_case_analysis(
                current.requested_by,
                case.pk,
                submission_id=uuid5(current.pk, "vehicle-import-analysis"),
                scope={"type": "vehicle", "id": str(current.pk)}
                | analysis_scope(case, ["vehicle-location"]),
            )
        invalidate_vehicle_trips(
            current.requested_by,
            case,
            current.observations.values_list("vehicle_id", flat=True).distinct(),
        )


def process_import(import_id, job_token):
    """Claim once; old deliveries and completed confirmation retries are harmless."""
    with transaction.atomic():
        batch = (
            VehicleLocationImport.objects.select_for_update(of=("self",))
            .select_related("document")
            .filter(pk=import_id)
            .first()
        )
        if batch is None:
            return "unavailable"
        if str(batch.job_token) != str(job_token) or batch.status != "queued":
            return batch.status
        batch.status = "running"
        batch.started_at = timezone.now()
        VehicleLocationImport.objects.filter(pk=batch.pk).update(
            status="running", started_at=batch.started_at
        )
    started = time.monotonic()
    try:
        _authorize(batch)
        if batch.document.case_id != batch.case_id:
            raise ImportJobError("source_invalid")
        if batch.parser_version not in SUPPORTED_PARSER_VERSIONS:
            raise ImportJobError("version_unavailable")
        if batch.phase == "prepare":
            _prepare(batch, started)
        elif batch.phase == "publish":
            _publish(batch, started)
        else:
            raise ImportJobError("processing_failed")
        return VehicleLocationImport.objects.get(pk=batch.pk).status
    except SupersededImport:
        return "superseded"
    except PermissionDenied, Http404:
        code = "access_removed"
    except ImportJobError as error:
        code = error.code
    except OSError:
        code = "source_unavailable"
    except ValidationError:
        code = "source_invalid"
    except Exception:
        # Do not put private CSV values or parser exception messages in task logs.
        code = "processing_failed"
    recovery = {"status": "failed", "error_code": code, "finished_at": timezone.now()}
    if code == "vehicle_unavailable":
        recovery.update(status="review", phase="prepare")
    VehicleLocationImport.objects.filter(
        pk=batch.pk, job_token=batch.job_token, status="running"
    ).update(**recovery)
    return recovery["status"]

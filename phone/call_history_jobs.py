"""Retain, review, and publish a case's call history CSV."""

import hashlib
import time
from dataclasses import asdict
from itertools import batched
from uuid import UUID, uuid4

from allauth.account.models import EmailAddress
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import F, Q
from django.http import Http404
from django.tasks import task

from cases.permissions import get_case
from core.evidence.queries import scoped_evidence
from core.storage import (
    open_private_file,
    retain_uploaded_originals,
    staged_file_upload,
)
from document.models import DocumentEvidence
from people.models import Person
from phone.call_history import (
    MAX_ROWS,
    PARSER_VERSION,
    export_zone,
    import_file_limit,
    parse_call_history,
)
from phone.models import (
    CallHistoryImport,
    CallHistoryRow,
    CallImportSource,
    CallPersonLink,
    FaceTimeEvidence,
    PhoneCallEvidence,
)

CHUNK_SIZE = 250
MAX_JOB_SECONDS = 600
MAX_ATTEMPTS = 3
MAX_OVERLAP_LINKS = 100_000
OMIT = object()
CALL_TYPES = {"phone-call": PhoneCallEvidence, "facetime": FaceTimeEvidence}
ERROR_MESSAGES = {
    "processing_failed": ("Processing stopped. Try again after checking the worker."),
    "source_unavailable": (
        "The original CSV is unavailable. Restore the original and then retry."
    ),
    "source_archived": "Restore the original CSV before continuing.",
    "source_invalid": (
        "The retained CSV could not be verified or read. Check the required columns, "
        "UTF-8 encoding, and file limits."
    ),
    "access_removed": (
        "A case owner or editor with a verified email address can retry this import."
    ),
    "people_changed": (
        "A person choice is no longer available. Review the person selections."
    ),
    "overlaps_changed": (
        "Possible overlaps changed while this import was waiting. Review them again."
    ),
    "limit_exceeded": (
        "This import exceeded a processing limit. Split the CSV into smaller files."
    ),
}


class ImportFailure(Exception):
    def __init__(self, code):
        self.code = code


class SupersededImport(Exception):
    pass


def _uuid(value):
    try:
        return UUID(str(getattr(value, "pk", value)))
    except TypeError, ValueError:
        raise ValidationError("Choose a valid record.") from None


def _editor(actor, case_id, *, lock=False):
    case = get_case(actor, case_id, edit=True, lock=lock)
    if not EmailAddress.objects.filter(user=actor, verified=True).exists():
        raise PermissionDenied("Verify your email before processing an import.")
    return case


def _source_document(case, document):
    retained = DocumentEvidence.objects.filter(case=case, pk=_uuid(document)).first()
    if retained is None:
        raise ValidationError("Choose a CSV retained in this case.")
    if retained.is_archived:
        raise ValidationError(ERROR_MESSAGES["source_archived"])
    if not 0 < retained.byte_size <= import_file_limit():
        raise ValidationError("Choose a CSV within the call history import size limit.")
    return retained


def retain_call_history_upload(actor, case_id, *, submission_id, original):
    """Retain the CSV before the investigator picks a source for review."""
    submission_id = _uuid(submission_id)
    _editor(actor, case_id)
    limit = import_file_limit()
    with (
        staged_file_upload(original, limit=limit) as measurement,
        retain_uploaded_originals() as originals,
    ):
        case = _editor(actor, case_id, lock=True)
        if measurement is None or not 0 < measurement[1] <= limit:
            raise ValidationError("Choose a nonempty CSV within the import size limit.")
        checksum, byte_size = measurement
        previous = DocumentEvidence.objects.filter(
            case=case, uploaded_by=actor, submission_id=submission_id
        ).first()
        if previous is not None:
            if previous.sha256 != checksum or previous.byte_size != byte_size:
                raise ValidationError(
                    "This upload submission was already used for different material."
                )
            if previous.is_archived:
                raise ValidationError(ERROR_MESSAGES["source_archived"])
            return previous
        document = DocumentEvidence(
            case=case,
            uploaded_by=actor,
            submission_id=submission_id,
            file=original,
            title=f"Call history CSV · {original.name}"[:255],
        )
        document.full_clean()
        document.save()
        originals.append(document.file)
        return document


def _authorized_batch(batch, *, lock=False):
    case = _editor(batch.requested_by, batch.case_id, lock=lock)
    if batch.source.case_id != case.pk or batch.document.case_id != case.pk:
        raise ImportFailure("source_invalid")
    document = DocumentEvidence.objects.filter(case=case, pk=batch.document_id).first()
    if document is None or document.sha256 != batch.source_sha256:
        raise ImportFailure("source_invalid")
    if document.is_archived:
        raise ImportFailure("source_archived")
    if batch.parser_version != PARSER_VERSION:
        raise ImportFailure("source_invalid")
    return case


@transaction.atomic
def retain_call_history(
    actor, case_id, *, submission_id, document, source, source_label, timezone
):
    """Queue preview of a retained CSV once per case source and checksum."""
    submission_id = _uuid(submission_id)
    export_zone(timezone)
    source_label = (source_label or "").strip()
    if bool(source) == bool(source_label):
        raise ValidationError("Select an existing source or enter a new source label.")
    if len(source_label) > CallImportSource._meta.get_field("label").max_length:
        raise ValidationError("Enter a shorter source label.")
    case = _editor(actor, case_id, lock=True)
    document = _source_document(case, document)
    checksum = document.sha256
    previous = (
        CallHistoryImport.objects.filter(
            case=case, uploaded_by=actor, submission_id=submission_id
        )
        .select_related("source", "document")
        .first()
    )
    source_id = _uuid(source) if source else None
    if previous is not None:
        same_source = (
            previous.source_id == source_id
            if source_id
            else previous.source_label == source_label
        )
        if (
            not same_source
            or previous.source.case_id != case.pk
            or previous.document.case_id != case.pk
            or previous.source_sha256 != checksum
            or previous.timezone != timezone
        ):
            raise ValidationError(
                "This submission was already used for another import."
            )
        return previous
    if source_id:
        selected_source = CallImportSource.objects.filter(
            case=case, pk=source_id
        ).first()
        if selected_source is None:
            raise ValidationError("Choose a source in this case.")
    else:
        if CallImportSource.objects.filter(
            case=case, label__iexact=source_label
        ).exists():
            raise ValidationError(
                "Select the existing source or enter a distinct label."
            )
        selected_source = CallImportSource.objects.create(
            case=case, label=source_label, created_by=actor
        )
    previous = CallHistoryImport.objects.filter(
        case=case,
        source=selected_source,
        source_sha256=checksum,
        document__case=case,
    ).first()
    if previous is not None:
        if previous.timezone != timezone:
            raise ValidationError(
                "This source and CSV already have an import in another timezone."
            )
        return previous
    batch = CallHistoryImport.objects.create(
        case=case,
        document=document,
        source=selected_source,
        source_sha256=checksum,
        source_label=selected_source.label,
        uploaded_by=actor,
        requested_by=actor,
        submission_id=submission_id,
        timezone=timezone,
        parser_version=PARSER_VERSION,
        status="preparing",
    )
    process_call_history.enqueue(str(batch.pk), str(batch.attempt_id))
    return batch


def _locked_batch(actor, case_id, batch_id):
    case = _editor(actor, case_id, lock=True)
    batch = (
        CallHistoryImport.objects.select_for_update(of=("self",))
        .filter(pk=batch_id, case=case, document__case=case, source__case=case)
        .select_related("document", "source")
        .first()
    )
    if batch is None:
        raise Http404("Call history import not found.")
    if batch.document.is_archived:
        raise ValidationError(ERROR_MESSAGES["source_archived"])
    return batch


def _check_review(batch, reviewed_attempt):
    if batch.status != "review" or str(batch.attempt_id) != str(reviewed_attempt):
        raise ValidationError(
            "This review changed. Reopen it before saving your choices."
        )


def _person_choice(value):
    return str(_uuid(value)) if value else None


def _people_choices(selection):
    return {
        value
        for value in (
            selection.get("source_person"),
            *selection.get("group_people", {}).values(),
            *selection.get("row_people", {}).values(),
        )
        if value
    }


def _missing_people(case, candidates):
    return candidates - {
        str(person_id)
        for person_id in Person.objects.filter(
            case=case, pk__in=candidates
        ).values_list("pk", flat=True)
    }


def _clear_missing_people(batch, missing):
    selection = batch.selection
    if selection.get("source_person") in missing:
        selection["source_person"] = None
    groups = selection.get("group_people", {})
    for key, choice in list(groups.items()):
        if choice in missing:
            del groups[key]
    rows = selection.get("row_people", {})
    for key, choice in rows.items():
        if choice in missing:
            rows[key] = None
    return _finish(batch, "review", error="people_changed")


def _row_choices(batch, choices, *, overlaps=False):
    if not choices:
        return {}
    try:
        numbers = {int(number): choice for number, choice in choices.items()}
    except TypeError, ValueError, AttributeError:
        raise ValidationError("Choose calls from this review.") from None
    if not all(2 <= number <= MAX_ROWS + 1 for number in numbers):
        raise ValidationError("Choose calls from this review.")
    rows = batch.rows.filter(number__in=numbers, issue="")
    if overlaps:
        rows = rows.exclude(overlap_candidates=[])
    if set(rows.values_list("number", flat=True)) != set(numbers):
        raise ValidationError("Choose calls from this review.")
    return {str(number): choice for number, choice in numbers.items()}


@transaction.atomic
def save_call_history_selection(
    actor,
    case_id,
    batch_id,
    *,
    reviewed_attempt,
    source_person=OMIT,
    group_people=None,
    row_people=None,
    overlap_keep=None,
    overlap_rows=None,
):
    """Apply one page of choices to the reviewed import."""
    batch = _locked_batch(actor, case_id, batch_id)
    _check_review(batch, reviewed_attempt)
    selection = batch.selection
    choices = set()
    if source_person is not OMIT:
        choice = _person_choice(source_person)
        choices.add(choice)
        selection["source_person"] = choice
    if group_people:
        group_ids = set(group_people)
        available = set(
            batch.rows.filter(identifier__in=group_ids, issue="")
            .exclude(identifier="")
            .values_list("identifier", flat=True)
            .distinct()
        )
        if available != group_ids:
            raise ValidationError("Choose participant groups from this review.")
        groups = selection.setdefault("group_people", {})
        for identifier, value in group_people.items():
            choice = _person_choice(value)
            choices.add(choice)
            if choice:
                groups[identifier] = choice
            else:
                groups.pop(identifier, None)
    if row_people:
        picks = selection.setdefault("row_people", {})
        for row, value in _row_choices(batch, row_people).items():
            if value == "inherit":
                picks.pop(row, None)
            else:
                choice = _person_choice(value)
                choices.add(choice)
                picks[row] = choice
    if overlap_keep is not None:
        if not isinstance(overlap_keep, bool):
            raise ValidationError("Choose whether to keep possible overlaps.")
        selection["overlap_keep"] = overlap_keep
    if overlap_rows:
        overrides = selection.setdefault("overlap_rows", {})
        for row, value in _row_choices(batch, overlap_rows, overlaps=True).items():
            if value is None:
                overrides.pop(row, None)
            elif isinstance(value, bool):
                overrides[row] = value
            else:
                raise ValidationError("Choose whether to keep possible overlaps.")
    choices.discard(None)
    if _missing_people(batch.case, choices):
        raise ValidationError("Choose people in this case.")
    batch.attempt_id = uuid4()
    batch.save(update_fields=("selection", "attempt_id", "updated_at"))
    return batch


def _queue(batch, actor, status, attempts):
    batch.requested_by = actor
    batch.status = status
    batch.attempts = attempts
    batch.attempt_id = uuid4()
    batch.is_running = False
    batch.error = ""
    batch.save(
        update_fields=(
            "requested_by",
            "status",
            "attempts",
            "attempt_id",
            "is_running",
            "error",
            "updated_at",
        )
    )
    process_call_history.enqueue(str(batch.pk), str(batch.attempt_id))
    return batch


@transaction.atomic
def confirm_call_history(actor, case_id, batch_id, *, reviewed_attempt):
    batch = _locked_batch(actor, case_id, batch_id)
    if batch.status in ("publishing", "completed"):
        return batch
    _check_review(batch, reviewed_attempt)
    if (
        "overlap_keep" not in batch.selection
        and batch.rows.exclude(overlap_candidates=[]).exists()
    ):
        raise ValidationError(
            "Review possible overlaps and choose whether to keep them."
        )
    if missing := _missing_people(batch.case, _people_choices(batch.selection)):
        _clear_missing_people(batch, missing)
        return batch
    if not batch.rows.filter(issue="").exists():
        raise ValidationError("There are no ready calls to publish.")
    return _queue(batch, actor, "publishing", 1)


@transaction.atomic
def retry_call_history(actor, case_id, batch_id, *, worker_stopped=False):
    batch = _locked_batch(actor, case_id, batch_id)
    if batch.is_running and not worker_stopped:
        raise ValidationError("Confirm that the original worker has stopped.")
    if batch.status in ("prepare_failed", "publish_failed") or batch.is_running:
        if batch.attempts >= MAX_ATTEMPTS:
            raise ValidationError(
                "This processing step has reached its three-attempt limit."
            )
        status = "preparing" if batch.status.startswith("prepar") else "publishing"
        return _queue(batch, actor, status, batch.attempts + 1)
    return batch


def _read_original(batch):
    limit = import_file_limit()
    with open_private_file(batch.document, batch.case_id) as original:
        content = original.read(limit + 1)
    if (
        len(content) > limit
        or len(content) != batch.document.byte_size
        or hashlib.sha256(content).hexdigest() != batch.source_sha256
    ):
        raise ImportFailure("source_invalid")
    return content


def _check_time(started):
    if time.monotonic() - started > MAX_JOB_SECONDS:
        raise ImportFailure("limit_exceeded")


def _database_limits():
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '60s'")
        cursor.execute("SET LOCAL lock_timeout = '10s'")


def _current(batch):
    current = (
        CallHistoryImport.objects.select_for_update(of=("self",))
        .select_related("source", "document", "requested_by")
        .filter(pk=batch.pk, attempt_id=batch.attempt_id, is_running=True)
        .first()
    )
    if current is None:
        raise SupersededImport
    return current


def _review_overlaps(batch, rows, started):
    """Refresh overlap candidates on both preparation and publication."""
    counts = {}
    for row in rows:
        if row.fingerprint:
            counts[row.fingerprint] = counts.get(row.fingerprint, 0) + 1
    found = {}
    links = 0
    visible_calls = Q(
        phonecallevidence__in=scoped_evidence(batch.case, "phone-call").order_by()
    ) | Q(facetimeevidence__in=scoped_evidence(batch.case, "facetime").order_by())
    for fingerprints in batched(counts, CHUNK_SIZE, strict=False):
        _check_time(started)
        candidates = (
            CallHistoryRow.objects.filter(
                batch__case_id=batch.case_id,
                batch__source_id=batch.source_id,
                batch__source__case_id=batch.case_id,
                batch__document__case_id=batch.case_id,
                batch__source_sha256=F("batch__document__sha256"),
                batch__status="completed",
                disposition="published",
                fingerprint__in=fingerprints,
            )
            .exclude(batch=batch)
            .filter(visible_calls)
            .order_by("fingerprint", "batch_id", "number")
            .values_list("fingerprint", "pk")
        )
        for fingerprint, candidate_id in candidates.iterator(chunk_size=CHUNK_SIZE):
            found.setdefault(fingerprint, []).append(str(candidate_id))
            links += counts[fingerprint]
            if links > MAX_OVERLAP_LINKS:
                raise ImportFailure("limit_exceeded")
    changed = []
    for row in rows:
        candidates = found.get(row.fingerprint, [])
        if row.overlap_candidates != candidates:
            row.overlap_candidates = candidates
            changed.append(row)
    return changed


def _finish(batch, status, *, error=""):
    batch.is_running = False
    batch.status = status
    batch.error = error
    batch.attempt_id = uuid4()
    batch.save(
        update_fields=(
            "is_running",
            "status",
            "error",
            "attempt_id",
            "selection",
            "updated_at",
        )
    )
    return status


def _prepare(batch, started):
    rows = []
    for parsed in parse_call_history(_read_original(batch), batch.timezone):
        _check_time(started)
        rows.append(
            CallHistoryRow(
                batch=batch,
                disposition="held" if parsed.issue else "",
                **asdict(parsed),
            )
        )
    with transaction.atomic():
        _database_limits()
        _authorized_batch(batch, lock=True)
        current = _current(batch)
        _review_overlaps(current, rows, started)
        current.rows.all().delete()
        CallHistoryRow.objects.bulk_create(rows, batch_size=CHUNK_SIZE)
        return _finish(current, "review")


def _publication_records(batch, rows, started):
    selection = batch.selection
    source_person = selection.get("source_person")
    grouped_people = selection.get("group_people", {})
    row_people = selection.get("row_people", {})
    overlap_keep = selection.get("overlap_keep", True)
    overlap_rows = selection.get("overlap_rows", {})
    calls = {kind: [] for kind in CALL_TYPES}
    links = []
    changed = []
    resolved = False
    for row in rows:
        _check_time(started)
        if row.issue or row.disposition:
            continue
        row_number = str(row.number)
        changed.append(row)
        if row.overlap_candidates and not overlap_rows.get(row_number, overlap_keep):
            row.disposition = "skipped"
            continue
        counterparty = row_people.get(row_number, grouped_people.get(row.identifier))
        sender, recipient = (
            (source_person, counterparty)
            if row.direction == "outgoing"
            else (counterparty, source_person)
        )
        resolved = resolved or bool(sender and recipient)
        kind_label = "Phone call" if row.kind == "phone-call" else "FaceTime"
        item = CALL_TYPES[row.kind](
            case_id=batch.case_id,
            uploaded_by_id=batch.requested_by_id,
            import_row=row,
            title=f"{kind_label} · {row.identifier or 'Unknown caller'}"[:255],
            source=batch.document.source,
            occurred_at=row.occurred_at,
            duration=row.duration,
            sender_id=sender,
            recipient_id=recipient,
        )
        # Preparation validates immutable values. Review validates people.
        item._prepare_original()
        calls[row.kind].append(item)
        row.disposition = "published"
        for role, person in (("sender", sender), ("recipient", recipient)):
            if person:
                links.append(
                    CallPersonLink(
                        row=row,
                        role=role,
                        person_id=person,
                        linked_by_id=batch.requested_by_id,
                    )
                )
    for kind, records in calls.items():
        CALL_TYPES[kind].objects.bulk_create(records, batch_size=CHUNK_SIZE)
    CallPersonLink.objects.bulk_create(links, batch_size=CHUNK_SIZE)
    CallHistoryRow.objects.bulk_update(changed, ("disposition",), batch_size=CHUNK_SIZE)
    return resolved


def _publish(batch, started):
    _read_original(batch)
    with transaction.atomic():
        _database_limits()
        case = _authorized_batch(batch, lock=True)
        current = _current(batch)
        rows = list(current.rows.order_by("number"))
        differences = _review_overlaps(current, rows, started)
        if differences:
            CallHistoryRow.objects.bulk_update(
                differences, ("overlap_candidates",), batch_size=CHUNK_SIZE
            )
            current.selection.pop("overlap_keep", None)
            current.selection.pop("overlap_rows", None)
            return _finish(current, "review", error="overlaps_changed")
        if missing := _missing_people(case, _people_choices(current.selection)):
            return _clear_missing_people(current, missing)
        resolved = _publication_records(current, rows, started)
        status = _finish(current, "completed")
        if resolved:
            from core.analysis.automatic import start_call_history_analysis

            start_call_history_analysis(current.requested_by, current)
        return status


@task
def process_call_history(batch_id, attempt_id):
    """Claim one delivery and commit the private processing outcome."""
    with transaction.atomic():
        batch = (
            CallHistoryImport.objects.select_for_update(of=("self",))
            .select_related("source", "document", "requested_by")
            .filter(pk=batch_id)
            .first()
        )
        if batch is None:
            return "unavailable"
        if (
            str(batch.attempt_id) != str(attempt_id)
            or batch.is_running
            or batch.status not in ("preparing", "publishing")
        ):
            return batch.status
        batch.is_running = True
        CallHistoryImport.objects.filter(pk=batch.pk).update(is_running=True)
    started = time.monotonic()
    try:
        _authorized_batch(batch)
        if batch.status == "preparing":
            return _prepare(batch, started)
        return _publish(batch, started)
    except SupersededImport:
        return "superseded"
    except PermissionDenied, Http404:
        code = "access_removed"
    except ImportFailure as error:
        code = error.code
    except OSError:
        code = "source_unavailable"
    except ValidationError:
        code = "source_invalid"
    except Exception:
        # Task errors must not log source values or file storage details.
        code = "processing_failed"
    status = "prepare_failed" if batch.status == "preparing" else "publish_failed"
    CallHistoryImport.objects.filter(pk=batch.pk, attempt_id=batch.attempt_id).update(
        status=status, is_running=False, error=code
    )
    return status

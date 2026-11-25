"""Retain investigator submissions and manage the active evidence boundary."""

from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

from cases.permissions import get_case
from core.analysis.automatic import start_added_evidence_analysis
from core.analysis.errors import AnalysisError
from core.evidence.form_registry import evidence_form
from core.evidence.forms import EvidenceForm
from core.evidence.queries import duplicate_matches, get_evidence
from core.evidence.types import CALL_KINDS, FILE_KINDS, KINDS
from core.models.evidence import Evidence
from core.storage import retain_uploaded_originals, staged_file_upload
from phone.call_attachments import call_content_matches, save_call_content

DUPLICATE_SALT = "core.evidence.duplicate"


class SubmissionConflict(Exception):
    pass


class SubmissionUnavailable(Exception):
    def __init__(self, form, *, storage=False):
        super().__init__()
        self.form = form
        self.storage = storage


@dataclass(frozen=True)
class DuplicateReview:
    matches: list[dict]
    confirmation: str


@dataclass(frozen=True)
class EvidenceSubmission:
    form: EvidenceForm
    item: Evidence | None = None
    created: bool = False
    duplicates: DuplicateReview | None = None


def _duplicate_review(actor, case, kind, item, confirmation):
    if kind not in (*FILE_KINDS, "text"):
        return None
    identity = [str(case.pk), str(actor.pk), kind, item.sha256]
    try:
        if signing.loads(confirmation, salt=DUPLICATE_SALT) == identity:
            return None
    except signing.BadSignature:
        pass
    matches = list(duplicate_matches(case, item.sha256)[:11])
    if matches:
        return DuplicateReview(matches, signing.dumps(identity, salt=DUPLICATE_SALT))
    return None


def _matches_submission(previous, form, kind, submission_id):
    if previous.sha256 != form.instance.sha256:
        return False
    if kind in CALL_KINDS:
        earlier_content = previous.attachments.filter(
            case_id=previous.case_id,
            uploaded_by_id=previous.uploaded_by_id,
            submission_id=submission_id,
        ).first()
        return call_content_matches(earlier_content, form.cleaned_data)
    return True


def submit_evidence(actor, case_id, kind, data, files):
    """Validate and retain one product submission under the current case edit lock."""
    try:
        submission_id = UUID(data.get("submission_id", ""))
    except ValueError, AttributeError:
        submission_id = None
    model = KINDS[kind]
    form = None
    try:
        with (
            staged_file_upload(
                files.get("file"), limit=settings.PANDECT_MAX_FILE_BYTES
            ),
            retain_uploaded_originals() as originals,
        ):
            case = get_case(actor, case_id, edit=True, lock=True)
            previous = (
                model.objects.filter(
                    case=case, uploaded_by=actor, submission_id=submission_id
                ).first()
                if submission_id
                else None
            )
            instance = model(case=case, uploaded_by=actor, submission_id=submission_id)
            form = evidence_form(kind)(data, files, kind=kind, instance=instance)
            if not form.is_valid():
                return EvidenceSubmission(form)
            if previous:
                if not _matches_submission(previous, form, kind, submission_id):
                    raise SubmissionConflict(
                        "This submission belongs to different material."
                    )
                return EvidenceSubmission(form, item=previous)
            duplicates = _duplicate_review(
                actor, case, kind, form.instance, data.get("duplicate_confirmation", "")
            )
            if duplicates:
                return EvidenceSubmission(form, duplicates=duplicates)

            item = form.save()
            if kind == "vehicle-location":
                from geo.trip_processing import invalidate_vehicle_trips

                invalidate_vehicle_trips(actor, case, [item.vehicle_id])
            original = getattr(item, "file", None)
            if original:
                originals.append(original)
            attachment = None
            if kind in CALL_KINDS:
                attachment = save_call_content(
                    item, actor, form.cleaned_data, submission_id
                )
                if attachment and attachment.file:
                    originals.append(attachment.file)
            start_added_evidence_analysis(
                actor,
                case.pk,
                kind,
                item.pk,
                attachment_id=attachment.pk if attachment else None,
                new_call=kind in CALL_KINDS,
            )
    except OSError as exc:
        raise SubmissionUnavailable(form, storage=True) from exc
    except IntegrityError as exc:
        raise SubmissionConflict(
            "This submission could not be confirmed. Retry with the same submission ID."
        ) from exc
    except (AnalysisError, ValidationError, DatabaseError) as exc:
        raise SubmissionUnavailable(form) from exc
    return EvidenceSubmission(form, item=item, created=True)


def set_archive_attribution(item, actor, archived):
    item.is_archived = archived
    item.archive_changed_at = timezone.now()
    item.archive_changed_by = actor


def refresh_archive_derivatives(case, actor, kind, item):
    """Refresh dependent state after a real transition in the same transaction."""
    from core.case_search.indexing import evidence_archive_changed

    evidence_archive_changed(case, actor, kind, item.pk, archived=item.is_archived)
    if kind == "vehicle-location":
        from geo.trip_processing import invalidate_vehicle_trips

        invalidate_vehicle_trips(actor, case, [item.vehicle_id])


@transaction.atomic
def set_evidence_archive(actor, case_id, kind, evidence_id, *, archived):
    case = get_case(actor, case_id, edit=True, lock=True)
    item = get_evidence(case, kind, evidence_id, lock=True)
    if item.is_archived != archived:
        set_archive_attribution(item, actor, archived)
        item.save(
            update_fields=("is_archived", "archive_changed_at", "archive_changed_by")
        )
        refresh_archive_derivatives(case, actor, kind, item)
    return case, item

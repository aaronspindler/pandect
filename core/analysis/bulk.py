"""Prepare local checks for all active evidence in bounded background steps."""

from uuid import uuid5

from allauth.account.models import EmailAddress
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404

from cases.permissions import get_case
from core.analysis.automatic import start_evidence_analysis
from core.analysis.errors import AnalysisError, error_message
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, KINDS
from core.models import CaseAnalysisBatch
from phone.models import CallEvidenceAttachment

SOURCES_PER_STEP = 25


@transaction.atomic
def start_case_analysis(actor, case_id, *, submission_id, scope=None):
    case = get_case(actor, case_id, edit=True, lock=True)
    scope = scope or {}
    if scope:
        from core.analysis.import_checks import imported_sources

        if not imported_sources(case, scope)[1].exists():
            raise ValidationError("This import has no newly added evidence to check.")
    existing = CaseAnalysisBatch.objects.filter(pk=submission_id).first()
    if existing:
        if (
            existing.case_id != case.pk
            or existing.requested_by_id != actor.pk
            or existing.scope != scope
        ):
            raise ValidationError(
                "This submission was already used for another analysis."
            )
        return existing, False
    try:
        with transaction.atomic():
            batch = CaseAnalysisBatch.objects.create(
                pk=submission_id, case=case, requested_by=actor, scope=scope
            )
    except IntegrityError:
        raise ValidationError(
            "This submission was already used for another analysis."
        ) from None
    from core.analysis.tasks import prepare_case_analysis

    prepare_case_analysis.enqueue(str(batch.pk))
    return batch, True


def _next_sources(batch, case):
    if batch.scope:
        from core.analysis.import_checks import imported_sources

        kind, originals = imported_sources(case, batch.scope)
        batch.source_kind = kind
        originals = originals.filter(created_at__lte=batch.created_at)
        if batch.source_cursor:
            originals = originals.filter(pk__gt=batch.source_cursor)
        return [
            (pk, pk)
            for pk in originals.order_by("pk").values_list("pk", flat=True)[
                : min(500, settings.PANDECT_ANALYSIS_MAX_SOURCES)
            ]
        ]
    for kind in sorted(KINDS):
        if kind < batch.source_kind:
            continue
        originals = scoped_evidence(case, kind).filter(
            is_archived=False, created_at__lte=batch.created_at
        )
        if kind in CALL_KINDS:
            parent = "phone_call" if kind == "phone-call" else "facetime"
            sources = CallEvidenceAttachment.objects.filter(
                case=case,
                created_at__lte=batch.created_at,
                **{f"{parent}__in": originals.values("pk")},
            )
            fields = ("pk", f"{parent}_id")
        else:
            sources = originals
            fields = ("pk", "pk")
        if kind == batch.source_kind and batch.source_cursor:
            sources = sources.filter(pk__gt=batch.source_cursor)
        rows = list(sources.order_by("pk").values_list(*fields)[:SOURCES_PER_STEP])
        if rows:
            batch.source_kind = kind
            return rows
    return []


def prepare_case_analysis_batch(batch_id):
    """Commit a page of runs, its cursor and its continuation task together."""
    try:
        with transaction.atomic():
            batch = (
                CaseAnalysisBatch.objects.select_related("requested_by")
                .filter(pk=batch_id)
                .first()
            )
            if batch is None:
                return "unavailable"
            if batch.status != CaseAnalysisBatch.Status.QUEUED:
                return batch.status
            case = get_case(batch.requested_by, batch.case_id, edit=True, lock=True)
            if not EmailAddress.objects.filter(
                user=batch.requested_by, verified=True
            ).exists():
                raise PermissionDenied
            batch = CaseAnalysisBatch.objects.select_for_update().get(pk=batch_id)
            if batch.status != CaseAnalysisBatch.Status.QUEUED:
                return batch.status
            rows = _next_sources(batch, case)
            if batch.scope and rows:
                from core.analysis.import_checks import prepare_import_checks

                prepare_import_checks(batch, case, rows)
                batch.source_cursor = rows[-1][0]
                batch.processed_sources += len(rows)
                rows_to_prepare = []
            else:
                rows_to_prepare = rows
            for source_id, evidence_id in rows_to_prepare:
                attachment_id = source_id if batch.source_kind in CALL_KINDS else None
                try:
                    runs, _ = start_evidence_analysis(
                        batch.requested_by,
                        case.pk,
                        batch.source_kind,
                        evidence_id,
                        attachment_id=attachment_id,
                        submission_id=uuid5(
                            batch.pk, f"{batch.source_kind}:{source_id}"
                        ),
                        batch=batch,
                    )
                except (AnalysisError, ValidationError, Http404) as error:
                    batch.unavailable_sources += 1
                    if len(batch.preparation_errors) < 100:
                        batch.preparation_errors.append(
                            {
                                "kind": batch.source_kind,
                                "evidence_id": str(evidence_id),
                                "message": error_message(
                                    error.code
                                    if isinstance(error, AnalysisError)
                                    else "source_unavailable"
                                ),
                            }
                        )
                else:
                    batch.queued_runs += len(runs)
                batch.processed_sources += 1
                batch.source_cursor = source_id
            if rows:
                from core.analysis.tasks import prepare_case_analysis

                prepare_case_analysis.enqueue(str(batch.pk))
            else:
                batch.status = CaseAnalysisBatch.Status.COMPLETE
            batch.save()
            return batch.status
    except Http404, PermissionDenied:
        error = "access_removed"
    except Exception:
        # Keep operational exceptions and source details out of product messages.
        error = "preparation_failed"
    CaseAnalysisBatch.objects.filter(
        pk=batch_id, status=CaseAnalysisBatch.Status.QUEUED
    ).update(status=CaseAnalysisBatch.Status.FAILED, error_code=error)
    return error

"""Prepare local checks for all active evidence in bounded background steps."""

from datetime import datetime
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


def save_source_cursor(batch, row, time_field, **ordering):
    """Keep the ordering keys after a row disappears from a later page."""
    batch.source_cursor = row.pk
    if batch.scope.get("analysis_version"):
        moment = getattr(row, time_field)
        batch.scope["_cursor"] = {
            "time": moment.isoformat() if moment else None
        } | ordering


def retained_cursor_time(batch):
    value = batch.scope["_cursor"]["time"]
    return datetime.fromisoformat(value) if value else None


@transaction.atomic
def resume_case_analysis(actor, case_id, batch_id):
    """Resume a stopped preparation after its last committed page."""
    case = get_case(actor, case_id, edit=True, lock=True)
    batch = (
        CaseAnalysisBatch.objects.select_for_update()
        .filter(
            pk=batch_id,
            case=case,
            requested_by=actor,
            scope__analysis_version=2,
        )
        .first()
    )
    if batch is None:
        raise Http404("Analysis batch unavailable.")
    if not EmailAddress.objects.filter(user=actor, verified=True).exists():
        raise PermissionDenied
    if batch.status != CaseAnalysisBatch.Status.FAILED:
        return batch, False
    batch.status = CaseAnalysisBatch.Status.QUEUED
    batch.error_code = ""
    batch.save(update_fields=("status", "error_code"))
    from core.analysis.tasks import prepare_case_analysis

    prepare_case_analysis.enqueue(str(batch.pk))
    return batch, True


@transaction.atomic
def start_case_analysis(actor, case_id, *, submission_id, scope):
    case = get_case(actor, case_id, edit=True, lock=True)
    if not scope:
        raise ValidationError("Choose an analysis scope.")
    if scope.get("type") not in {
        "added",
        "reprocess",
        "instance_summaries",
        "search_generation",
        "failed_retries",
        "conversation_grouping",
    }:
        from core.analysis.import_checks import imported_sources

        if not imported_sources(case, scope)[1].exists():
            raise ValidationError("This import has no newly added evidence to check.")
    existing = CaseAnalysisBatch.objects.filter(pk=submission_id).first()
    if existing:
        if (
            existing.case_id != case.pk
            or existing.requested_by_id != actor.pk
            or {key: value for key, value in existing.scope.items() if key != "_cursor"}
            != scope
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
    # Previously queued local batches have no scope. Resume their original order.
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


def _prepare_evidence_step(batch, case):
    if batch.scope.get("type") == "messages" and batch.scope.get("automatic") is True:
        from phone.analysis.ingest import next_messages, prepare_import_analysis

        selected = next_messages(batch, case)
        if selected:
            prepare_import_analysis(batch, case, selected)
            save_source_cursor(batch, selected[-1], "occurred_at")
            batch.source_kind = "text-message"
            batch.processed_sources += len(selected)
        return bool(selected)

    rows = _next_sources(batch, case)
    if batch.scope and rows:
        from core.analysis.import_checks import prepare_import_checks

        prepare_import_checks(batch, case, rows)
        batch.source_cursor = rows[-1][0]
        batch.processed_sources += len(rows)
        return True

    for source_id, evidence_id in rows:
        attachment_id = source_id if batch.source_kind in CALL_KINDS else None
        try:
            runs, _ = start_evidence_analysis(
                batch.requested_by,
                case.pk,
                batch.source_kind,
                evidence_id,
                attachment_id=attachment_id,
                submission_id=uuid5(batch.pk, f"{batch.source_kind}:{source_id}"),
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
    return bool(rows)


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
            scope_type = batch.scope.get("type")
            grouping_phase = scope_type == "conversation_grouping" or batch.scope.get(
                "_cursor", {}
            ).get("groupings")
            if grouping_phase:
                from phone.grouping_jobs import prepare_grouping_step

                has_more = prepare_grouping_step(batch, case)
            elif scope_type == "search_generation":
                from core.case_search.generation import prepare_step

                has_more = prepare_step(batch, case)
            elif scope_type == "instance_summaries":
                from phone.instance_summaries import prepare_instance_summaries

                has_more = prepare_instance_summaries(batch, case)
            elif scope_type == "failed_retries":
                from core.analysis.retries import prepare_failed_retries

                has_more = prepare_failed_retries(batch, case)
            elif scope_type == "reprocess":
                from core.analysis.reprocessing import prepare_reprocessing

                has_more = prepare_reprocessing(batch, case)
            elif scope_type == "added":
                from core.analysis.reprocessing import prepare_added_evidence

                has_more = prepare_added_evidence(batch, case)
            elif scope_type == "vehicle" and batch.scope.get("analysis_version"):
                from core.analysis.reprocessing import prepare_vehicle_import

                has_more = prepare_vehicle_import(batch, case)
            else:
                has_more = _prepare_evidence_step(batch, case)
            if (
                not has_more
                and not grouping_phase
                and scope_type in {"messages", "reprocess", "added"}
                and batch.grouping_requests.filter(
                    case=case,
                    grouping__case=case,
                    grouping__published_at__isnull=True,
                    error_code="",
                ).exists()
            ):
                from phone.grouping_jobs import prepare_grouping_step

                batch.scope["_cursor"] = {"groupings": True}
                has_more = prepare_grouping_step(batch, case)
            if has_more:
                from core.analysis.tasks import prepare_case_analysis

                prepare_case_analysis.enqueue(str(batch.pk))
            else:
                batch.status = CaseAnalysisBatch.Status.COMPLETE
            batch.save()
            return batch.status
    except Http404, PermissionDenied:
        error = "access_removed"
    except AnalysisError as failure:
        error = failure.code
    except Exception:
        # Keep operational exceptions and source details out of product messages.
        error = "preparation_failed"
    CaseAnalysisBatch.objects.filter(
        pk=batch_id, status=CaseAnalysisBatch.Status.QUEUED
    ).update(status=CaseAnalysisBatch.Status.FAILED, error_code=error)
    return error

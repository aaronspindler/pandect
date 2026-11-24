"""Queue analysis for newly retained evidence and historical local checks."""

from uuid import NAMESPACE_URL, UUID, uuid5

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.analysis.services import start_run
from core.evidence.queries import get_evidence
from core.evidence.types import CALL_KINDS, LOCATION_KINDS
from core.models import AnalysisRun, CaseAnalysisBatch


def evidence_runs(case, kind, evidence_id):
    return AnalysisRun.objects.filter(
        case=case, inputs__contains=[{"kind": kind, "evidence_id": str(evidence_id)}]
    )


def _text_checks(selections):
    return [
        ("entities", selections, {}),
        ("text_topics", selections, {"include_summary": False}),
    ]


def _call_checks(case, item, original, attachment_id):
    attachments = item.attachments.filter(case=case)
    if attachment_id is not None:
        additions = [get_object_or_404(attachments, pk=attachment_id)]
    else:
        limit = min(50, settings.PANDECT_ANALYSIS_MAX_SOURCES)
        additions = list(attachments[: limit + 1])
        if len(additions) > limit:
            raise ValidationError(
                f"Local analysis supports up to {limit} retained call additions. "
                "Use advanced analysis to select a smaller set."
            )
    text = []
    recordings = []
    for addition in additions:
        selection = original | {"attachment_id": str(addition.pk)}
        if addition.transcript:
            text.append(selection)
        if addition.file:
            recordings.append(selection)
    plan = []
    if recordings:
        plan.append(("audio_metadata", recordings, {}))
    if text:
        plan.extend(_text_checks(text))
    return plan


def _plan(case, kind, item, attachment_id):
    original = {"kind": kind, "id": str(item.pk)}
    selections = [original]
    if kind in CALL_KINDS:
        return _call_checks(case, item, original, attachment_id)
    if kind in {"text", "text-message"}:
        return _text_checks(selections)
    if kind == "document":
        return [("native_text", selections, {"automatic_followup": True})]
    if kind == "image":
        return [
            ("image_metadata", selections, {}),
            ("ocr", selections, {"automatic_followup": True}),
            ("plate_ocr", selections, {}),
        ]
    if kind == "audio":
        return [("audio_metadata", selections, {})]
    if kind in LOCATION_KINDS:
        plan = [("location_quality", selections, {})]
        if item.address:
            plan.append(("normalize_addresses", selections, {}))
        return plan
    return []


@transaction.atomic
def start_added_evidence_analysis(
    actor, case_id, kind, evidence_id, *, attachment_id=None, new_call=False
):
    """Give newly added evidence an analysis identity independent of HTTP retries."""
    from core.analysis.bulk import start_case_analysis
    from core.analysis.reprocessing import analysis_scope

    case = get_case(actor, case_id, edit=True, lock=True)
    submission_id = uuid5(
        NAMESPACE_URL,
        f"pandect:analysis-on-add:{case_id}:{kind}:{evidence_id}:{attachment_id}",
    )
    return start_case_analysis(
        actor,
        case.pk,
        submission_id=submission_id,
        scope={
            "type": "added",
            "kind": kind,
            "id": str(evidence_id),
            **({"attachment_id": str(attachment_id)} if attachment_id else {}),
            **({"new_call": True} if new_call else {}),
        }
        | analysis_scope(case, [kind]),
    )


@transaction.atomic
def start_evidence_reprocessing(actor, case_id, kind, evidence_id, *, submission_id):
    """Queue the broad analysis suite for one explicitly selected original."""
    from core.analysis.bulk import start_case_analysis
    from core.analysis.reprocessing import analysis_scope

    case = get_case(actor, case_id, edit=True, lock=True)
    original = get_evidence(case, kind, evidence_id)
    scope = {"type": "added", "kind": kind, "id": str(original.pk), "reprocess": True}
    existing = CaseAnalysisBatch.objects.filter(
        pk=submission_id,
        case=case,
        requested_by=actor,
        scope__contains=scope,
    ).first()
    if existing:
        return existing, False
    if original.is_archived:
        raise AnalysisError("source_archived")
    return start_case_analysis(
        actor,
        case.pk,
        submission_id=submission_id,
        scope=scope | analysis_scope(case, [kind]),
    )


@transaction.atomic
def start_evidence_analysis(
    actor, case_id, kind, evidence_id, *, submission_id, attachment_id=None, batch=None
):
    """Atomically enqueue a local batch, or return the original batch on retry."""
    case = get_case(actor, case_id, edit=True, lock=True)
    item = get_evidence(case, kind, evidence_id)
    try:
        batch_id = UUID(str(submission_id))
    except ValueError, TypeError, AttributeError:
        raise ValidationError("Choose a valid analysis submission.") from None
    existing = AnalysisRun.objects.filter(submission_id=batch_id).first()
    if existing:
        if (
            existing.case_id != case.pk
            or existing.requested_by_id != actor.pk
            or existing.batch_id != getattr(batch, "pk", None)
            or existing.parameters.get("automatic_batch") != str(batch_id)
            or not existing.inputs
            or any(
                selected.get("kind") != kind
                or selected.get("evidence_id") != str(item.pk)
                or (
                    attachment_id is not None
                    and selected.get("attachment_id") != str(attachment_id)
                )
                for selected in existing.inputs
            )
        ):
            raise ValidationError(
                "This submission was already used for another analysis."
            )
        return list(
            AnalysisRun.objects.filter(
                case=case,
                requested_by=actor,
                parameters__automatic_batch=str(batch_id),
            ).order_by("created_at", "pk")
        ), False
    runs = []
    for index, (operation, selections, parameters) in enumerate(
        _plan(case, kind, item, attachment_id)
    ):
        run, _ = start_run(
            actor,
            case.pk,
            operation,
            selections,
            parameters=parameters | {"automatic_batch": str(batch_id)},
            submission_id=batch_id if index == 0 else uuid5(batch_id, operation),
            batch=batch,
        )
        runs.append(run)
    return runs, bool(runs)


def follow_up_extraction(run, result):
    """Publish local text work in the same transaction as its batch's extraction."""
    if (
        run.batch_id
        and run.batch.scope.get("type") in {"reprocess", "added"}
        and run.operation in {"native_text", "ocr", "transcription"}
        and run.parameters.get("automatic_followup")
    ):
        from core.analysis.reprocessing import follow_up_reprocessing

        follow_up_reprocessing(run, result)
        return
    batch = run.parameters.get("automatic_batch")
    if (
        run.operation not in {"native_text", "ocr"}
        or not run.parameters.get("automatic_followup")
        or not batch
        or result.data.get("outcome", "complete") not in {"complete", "partial"}
        or not any(x.get("text", "").strip() for x in result.data.get("items", []))
    ):
        return
    selections = [
        {
            "kind": selected["kind"],
            "id": selected["evidence_id"],
            "result_id": str(result.pk),
        }
        for selected in run.inputs
    ]
    for name, inputs, parameters in _text_checks(selections):
        start_run(
            run.requested_by,
            run.case_id,
            name,
            inputs,
            parameters=parameters | {"automatic_batch": batch},
            submission_id=uuid5(run.pk, name),
            previous_run=run,
            batch=run.batch,
        )

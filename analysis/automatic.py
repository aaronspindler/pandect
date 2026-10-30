"""One deliberate request runs the applicable local analyses of one evidence item."""

from uuid import UUID, uuid5

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from cases.permissions import get_case
from evidence.queries import get_evidence
from evidence.types import CALL_KINDS

from .errors import AnalysisError
from .models import AnalysisResult, AnalysisRun
from .operations import LOCATIONS, OPERATIONS
from .services import resolve_inputs, start_run

# These operations need an investigator's comparison, boundary, or account mapping.
MANUAL_OPERATIONS = {
    "style_change",
    "proximity",
    "location_conflicts",
    "conversation_import",
}
TEXT_OPERATIONS = {"entities", "copied_text", "text_topics", "style_profiles"}
EXTRACTIONS = {"native_text", "ocr", "transcription"}


def _extracted_sources(case, kind, item, supplied):
    """Use the latest usable retained extraction per original/recording source."""
    choices = []
    seen = {choice.get("attachment_id") for choice in supplied}
    results = (
        AnalysisResult.objects.filter(
            case=case,
            run__case=case,
            run__status__in=("complete", "partial"),
            run__operation__in=EXTRACTIONS,
            run__inputs__contains=[{"kind": kind, "evidence_id": str(item.pk)}],
        )
        .select_related("run")
        .order_by("-created_at", "pk")
    )
    if kind == "document":
        results = results.exclude(run__operation="native_text")
    for result in results[:50]:
        try:
            sources = resolve_inputs(result.run, case)
        except AnalysisError:
            continue
        for source in sources:
            attachment_id = str(source.attachment.pk) if source.attachment else None
            if (
                source.kind != kind
                or source.item.pk != item.pk
                or attachment_id in seen
                or not any(
                    finding.get("text", "").strip()
                    and source.id in finding.get("source_ids", [])
                    for finding in result.data.get("items", [])
                )
            ):
                continue
            choices.append(
                {
                    "kind": kind,
                    "id": str(item.pk),
                    "attachment_id": attachment_id,
                    "result_id": str(result.pk),
                }
            )
            seen.add(attachment_id)
    return choices


def _plan(case, kind, item):
    original = {"kind": kind, "id": str(item.pk)}
    text = [original] if kind in {"text", "text-message"} else []
    recordings = []
    if kind in CALL_KINDS:
        limit = min(50, settings.PANDECT_ANALYSIS_MAX_SOURCES)
        additions = list(
            item.attachments.filter(case=case).order_by("created_at", "pk")[: limit + 1]
        )
        if len(additions) > limit:
            raise ValidationError(
                f"Automatic analysis supports up to {limit} retained call additions. "
                "Use advanced analysis to select a smaller set."
            )
        for addition in additions:
            selected = original | {"attachment_id": str(addition.pk)}
            if addition.transcript:
                text.append(selected)
            if addition.file:
                recordings.append(selected)
    # Documents get a fresh native extraction, with exact-version follow-up below.
    if kind in {"document", "audio", *CALL_KINDS}:
        text.extend(_extracted_sources(case, kind, item, text))
    plan = []
    for name, specification in OPERATIONS.items():
        if (
            specification.external
            or name in MANUAL_OPERATIONS
            or kind not in specification.kinds
        ):
            continue
        parameters = {}
        if name in TEXT_OPERATIONS:
            selections = text
            if not selections or (name == "copied_text" and len(selections) < 2):
                continue
            if name == "text_topics":
                parameters["include_summary"] = False
        elif name == "ocr":
            if kind != "image":
                continue
            selections = [original]
        elif name == "audio_metadata":
            selections = recordings if kind in CALL_KINDS else [original]
            if not selections:
                continue
        elif name in {"sessions", "comm_patterns"}:
            if kind != "text-message":
                continue
            selections = [original]
        elif name == "conversation_link":
            # A single communication has no other exchange to compare.
            continue
        elif name in {"native_text", "image_metadata"} or kind in LOCATIONS:
            selections = [original]
        else:
            continue
        if name in {"native_text", "ocr"}:
            parameters["automatic_followup"] = True
        plan.append((name, selections, parameters))
    return plan


@transaction.atomic
def start_evidence_analysis(actor, case_id, kind, evidence_id, *, submission_id):
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
            or existing.parameters.get("automatic_batch") != str(batch_id)
            or not existing.inputs
            or any(
                selected.get("kind") != kind
                or selected.get("evidence_id") != str(item.pk)
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
        _plan(case, kind, item)
    ):
        run, _ = start_run(
            actor,
            case.pk,
            operation,
            selections,
            parameters=parameters | {"automatic_batch": str(batch_id)},
            submission_id=batch_id if index == 0 else uuid5(batch_id, operation),
        )
        runs.append(run)
    return runs, bool(runs)


def follow_up_extraction(run, result):
    """Publish local text work in the same transaction as its batch's extraction."""
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
    for name in ("entities", "text_topics"):
        parameters = {"automatic_batch": batch}
        if name == "text_topics":
            parameters["include_summary"] = False
        start_run(
            run.requested_by,
            run.case_id,
            name,
            selections,
            parameters=parameters,
            submission_id=uuid5(run.pk, name),
            previous_run=run,
        )

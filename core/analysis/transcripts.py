"""Choose one retained candidate for an automatic recording's text analysis."""

import hashlib
import json

from django.conf import settings
from django.db import transaction

from cases.models import Case
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch


def transcription_profile(private, external):
    """Fingerprint the two execution profiles used in a paired qualification."""
    pair = json.dumps([private, external], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(pair.encode()).hexdigest()


def transcript_group(run):
    if run.operation == "transcription" and run.batch_id:
        return run.parameters.get("_transcript_group")
    return None


def follow_up_transcription(run):
    """Queue dependent work inside the transcriber's terminal transaction."""
    group = transcript_group(run)
    if not group:
        return False
    select_transcript(run.batch_id, group)
    return True


@transaction.atomic
def select_transcript(batch_id, group):
    from core.analysis.reprocessing import follow_up_reprocessing

    case_id = (
        CaseAnalysisBatch.objects.filter(pk=batch_id)
        .values_list("case_id", flat=True)
        .first()
    )
    # Match the publication and batch-preparation lock order.
    if (
        case_id is None
        or not Case.objects.select_for_update().filter(pk=case_id).first()
    ):
        return
    batch = CaseAnalysisBatch.objects.select_for_update().filter(pk=batch_id).first()
    if batch is None:
        return
    attempts = AnalysisRun.objects.filter(
        case_id=case_id,
        batch=batch,
        operation="transcription",
        parameters___transcript_group=group,
    )
    if (
        AnalysisRun.objects.filter(
            case_id=case_id, batch=batch, previous_run__in=attempts
        )
        .exclude(operation="transcription")
        .exists()
    ):
        return

    candidates = {}
    for result in (
        AnalysisResult.objects.filter(
            case_id=case_id, run__in=attempts, run__status__in=["complete", "partial"]
        )
        .select_related("run")
        .order_by("run__created_at", "run_id")
        .iterator(chunk_size=2)
    ):
        execution = result.run.parameters.get("_execution", {})
        backend = execution.get("backend")
        if backend not in {"local", "gateway"} or backend in candidates:
            continue
        if any(item.get("text", "").strip() for item in result.data.get("items", [])):
            candidates[backend] = result
        if len(candidates) == 2:
            break
    preferred = candidates.get("local")
    external = candidates.get("gateway")
    qualification = settings.PANDECT_PREFER_EXTERNAL_TRANSCRIPTION_PROFILE
    prefer_external = False
    if preferred is not None and qualification:
        external_execution = (
            external.run.parameters["_execution"]
            if external is not None
            else attempts.filter(parameters___execution__backend="gateway")
            .order_by("created_at", "pk")
            .values_list("parameters___execution", flat=True)
            .first()
        )
        prefer_external = qualification == transcription_profile(
            preferred.run.parameters["_execution"], external_execution
        )
    if (preferred is None or prefer_external) and attempts.filter(
        status__in=["queued", "running"]
    ).exists():
        return
    if external is not None and (preferred is None or prefer_external):
        preferred = external
    if preferred is not None:
        follow_up_reprocessing(preferred.run, preferred)

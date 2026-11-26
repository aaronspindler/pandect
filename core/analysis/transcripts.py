"""Choose one retained candidate for an automatic recording's text analysis."""

from django.db import transaction

from cases.models import Case
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch


def transcript_group(run):
    if run.operation == "transcription" and run.batch_id:
        return run.parameters.get("_transcript_group")
    return None


def follow_up_transcription(run):
    """Queue dependent work inside the transcriber's terminal transaction."""
    group = transcript_group(run)
    if not group:
        return False
    select_transcript(run.case_id, run.batch_id, group)
    return True


@transaction.atomic
def select_transcript(case_id, batch_id, group):
    from core.analysis.reprocessing import follow_up_reprocessing

    # Match the publication and batch-preparation lock order.
    if not Case.objects.select_for_update().filter(pk=case_id).exists():
        return
    batch = (
        CaseAnalysisBatch.objects.select_for_update()
        .filter(pk=batch_id, case_id=case_id)
        .first()
    )
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

    results = (
        AnalysisResult.objects.filter(
            case_id=case_id,
            run__in=attempts,
            run__status__in=["complete", "partial"],
            run__parameters___execution__backend="gateway",
        )
        .select_related("run")
        .order_by("run__created_at", "run_id")
    )
    for result in results.iterator(chunk_size=2):
        if any(item.get("text", "").strip() for item in result.data.get("items", [])):
            follow_up_reprocessing(result.run, result)
            break

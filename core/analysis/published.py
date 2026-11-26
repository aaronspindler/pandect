"""Base queries for same-case published analysis and exact result history."""

from django.db.models import OuterRef, Q, Subquery

from core.models import AnalysisResult, AnalysisRun, ExtractedPassage

SUCCESSFUL = ("complete", "partial", "empty")
PASSAGE_OPERATIONS = ("native_text", "ocr", "plate_ocr", "transcription")


def successful_results(case):
    return AnalysisResult.objects.filter(
        case=case, run__case=case, run__status__in=SUCCESSFUL
    ).filter(Q(run__previous_run__isnull=True) | Q(run__previous_run__case=case))


def latest_successful_run_id(case, outer_equivalence_field):
    """Select the latest equivalent published run before applying display filters."""
    runs = (
        AnalysisRun.objects.filter(
            case=case,
            result__case=case,
            status__in=SUCCESSFUL,
            equivalence_key=OuterRef(outer_equivalence_field),
        )
        .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
        .order_by("-created_at", "-pk")
    )
    return Subquery(runs.values("pk")[:1])


def published_passages(case):
    return ExtractedPassage.objects.filter(
        case=case,
        result__case=case,
        result__run__case=case,
        result__run__status__in=("complete", "partial"),
    )

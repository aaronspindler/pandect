"""Compact status for the case navigation and live collection refreshes."""

from django.db.models import Count, Exists
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_safe

from cases.models import Case
from cases.permissions import get_case
from config.access import product_login_required
from core.analysis.processing import (
    ACTIVE_IMPORTS,
    ACTIVE_RUNS,
    ACTIVE_TRIPS,
    case_imports,
    case_trip_generations,
)
from core.case_search.approval import search_approval_current
from core.case_search.semantic_model import is_external, profile
from core.models import AnalysisRun, CaseAnalysisBatch, CaseSearchIndex
from socialmedia.models import InstagramCheck


@product_login_required
@require_safe
def stream_unavailable(request, case_id):
    """WSGI-only installations tell EventSource to use the snapshot fallback."""
    get_case(request.user, case_id)
    return HttpResponse(status=204)


@product_login_required
@require_safe
def status(request, case_id):
    case = get_case(request.user, case_id)
    runs = (
        AnalysisRun.objects.filter(case=case, status__in=ACTIVE_RUNS)
        .values("status")
        .annotate(count=Count("*"))
        .order_by()
    )
    counts = {row["status"]: row["count"] for row in runs}
    active = bool(counts)
    if not active:
        pending = (
            Exists(CaseAnalysisBatch.objects.filter(case=case, status="queued"))
            | Exists(case_imports(case).filter(status__in=ACTIVE_IMPORTS))
            | Exists(case_trip_generations(case).filter(ACTIVE_TRIPS))
            | Exists(
                InstagramCheck.objects.filter(monitor__case=case, status="running")
            )
        )
        if (identity := profile()) and (
            not is_external() or search_approval_current(case)
        ):
            pending |= Exists(
                CaseSearchIndex.objects.filter(
                    case=case, profile=identity, status__in=ACTIVE_RUNS
                )
            )
        active = Case.objects.filter(pending, pk=case.pk).exists()
    response = JsonResponse(
        {
            "queued": counts.get("queued", 0),
            "running": counts.get("running", 0),
            "active": active,
            "versions": case.activity.versions(),
        }
    )
    response["Cache-Control"] = "private, no-store"
    return response

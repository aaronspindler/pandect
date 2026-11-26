"""Compact status for the case navigation and live collection refreshes."""

from uuid import UUID

from django.db.models import Count, Exists
from django.db.models.fields.json import KT
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_safe

from cases.models import Case
from cases.permissions import get_case, get_case_with_role
from config.access import product_login_required
from core.analysis.processing import (
    ACTIVE_CALL_IMPORTS,
    ACTIVE_IMPORTS,
    ACTIVE_RUNS,
    ACTIVE_TRIPS,
    case_call_imports,
    case_imports,
    case_trip_generations,
)
from core.case_search.approval import search_approval_current
from core.case_search.semantic_model import is_external, profile
from core.models import AnalysisRun, CaseAnalysisBatch, CaseSearchIndex, ChatTurn
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
    case, role = get_case_with_role(request.user, case_id)
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
            | Exists(
                ChatTurn.objects.filter(
                    case=case, chat__case=case, status__in=ACTIVE_RUNS
                )
            )
            | Exists(case_imports(case).filter(status__in=ACTIVE_IMPORTS))
            | Exists(case_call_imports(case).filter(status__in=ACTIVE_CALL_IMPORTS))
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
    snapshot = {
        "role": role,
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "active": active,
        "versions": case.activity.versions(),
    }
    if chat := request.GET.get("chat"):
        try:
            chat_id = UUID(chat)
        except ValueError:
            chat_id = None
        progress = (
            ChatTurn.objects.filter(case=case, chat__case=case, chat_id=chat_id)
            .order_by("-ordinal")
            .values_list(
                "pk",
                "status",
                "mode",
                "reviewed",
                "total",
                "gaps",
                KT("activity__revision"),
            )
            .first()
        )
        snapshot["chat_progress"] = progress[:-1] if progress else None
        snapshot["chat_activity"] = progress[-1] if progress else None
    response = JsonResponse(snapshot)
    response["Cache-Control"] = "private, no-store"
    return response

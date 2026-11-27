from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST, require_safe

from cases.permissions import case_context, get_case, require_verified_email
from config.access import product_login_required
from core.analysis.navigation import finding_url
from core.models import AnalysisReview
from core.relationships import open_edge, reviewed_edges


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    page = Paginator(
        reviewed_edges(case).select_related("result").order_by("-available_at", "pk"),
        25,
    ).get_page(request.GET.get("page"))
    for edge in page:
        material = open_edge(case, edge, None)
        edge.source_url = material.url if material else ""
        edge.finding_url = finding_url(case.pk, edge.result.run_id, edge.item_index)
    return render(
        request,
        "core/analysis/relationships.html",
        case_context(case, request.user)
        | {"page_obj": page, "nav_section": "connections"},
    )


@product_login_required
@require_POST
@transaction.atomic
def review(request, case_id, edge_id):
    case = get_case(request.user, case_id, edit=True, lock=True)
    require_verified_email(request.user)
    edge = get_object_or_404(reviewed_edges(case), pk=edge_id)
    decision = request.POST.get("decision")
    if decision not in AnalysisReview.Decision.values or not open_edge(
        case, edge, None
    ):
        raise Http404("Relationship unavailable.")
    if decision != edge.decision:
        AnalysisReview.objects.create(
            case=case,
            result=edge.result,
            item_index=edge.item_index,
            reviewed_by=request.user,
            decision=decision,
        )
    page = request.POST.get("page", "")
    return redirect(
        reverse(
            "core:analysis:relationships",
            args=(case.pk,),
            query={"page": page}
            if len(page) <= 9 and page.isascii() and page.isdecimal()
            else None,
            fragment=f"relationship-{edge.pk}",
        )
    )

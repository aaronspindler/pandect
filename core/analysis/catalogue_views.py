"""Read-only complete findings navigation."""

from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.catalogue import catalogue_page
from ui.live import render_live


@product_login_required
@require_safe
def browse(request, case_id):
    case = get_case(request.user, case_id)
    context = case_context(case, request.user) | catalogue_page(
        case, request.GET, actor=request.user, return_to_list=True
    )
    response = render_live(
        request,
        "core/analysis/catalogue.html",
        context | {"nav_section": "analysis"},
        collection=True,
    )
    response["Cache-Control"] = "private, no-store"
    return response

"""Read-only complete findings navigation."""

from django.shortcuts import render
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.catalogue import catalogue_page


@product_login_required
@require_safe
def browse(request, case_id):
    case = get_case(request.user, case_id)
    context = case_context(case, request.user) | catalogue_page(
        case, request.GET, return_to_list=True
    )
    response = render(
        request, "core/analysis/catalogue.html", context | {"nav_section": "analysis"}
    )
    response["Cache-Control"] = "private, no-store"
    return response

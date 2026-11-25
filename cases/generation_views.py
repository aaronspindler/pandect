"""Case owner's background search-preparation settings."""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from config.access import product_login_required

from .search_settings import change_preparation


@product_login_required
@require_POST
def update(request, case_id):
    outcome, label = change_preparation(
        request.user,
        case_id,
        request.POST.get("feature"),
        request.POST.get("action"),
        request.POST.get("confirmation", ""),
    )
    if outcome == "stale":
        messages.error(request, "The settings changed. Review the current destination.")
    elif outcome == "enable":
        messages.success(
            request, f"{label} enabled. Background preparation has been queued."
        )
    elif outcome == "check":
        messages.success(request, "Background preparation will check for missing work.")
    elif outcome == "disable":
        messages.success(request, f"{label} disabled for future processing.")
    else:
        messages.error(
            request, "Review the selected service before changing search preparation."
        )
    return redirect(
        reverse(
            "cases:case_settings",
            args=(case_id,),
            query={"review_search": "1"},
            fragment="search-preparation",
        )
    )

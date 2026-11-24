"""Case owner's background search-preparation settings."""

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_POST

from cases.permissions import get_case
from config.access import product_login_required
from core.case_search.generation import (
    OPERATIONS,
    enqueue_generation,
    set_policy,
    settings_rows,
)


@product_login_required
@require_POST
@transaction.atomic
def update(request, case_id):
    case = get_case(request.user, case_id, owner=True, lock=True)
    feature = request.POST.get("feature")
    if feature not in OPERATIONS:
        raise Http404("Unknown search setting.")
    review = next(
        row for row in settings_rows(case, request.user) if row["feature"] == feature
    )
    action = request.POST.get("action")
    if not constant_time_compare(
        request.POST.get("confirmation", ""), review["confirmation"]
    ):
        messages.error(request, "The settings changed. Review the current destination.")
    elif action == "enable" and review["ready"] and not review["enabled"]:
        set_policy(case, feature, True)
        messages.success(
            request,
            f"{review['label']} enabled. Background preparation has been queued.",
        )
    elif action == "check" and review["current"]:
        enqueue_generation(case, feature)
        messages.success(request, "Background preparation will check for missing work.")
    elif action == "disable" and review["enabled"]:
        set_policy(case, feature, False)
        messages.success(request, f"{review['label']} disabled for future processing.")
    else:
        messages.error(
            request, "Review the selected service before changing search preparation."
        )
    return redirect(
        reverse(
            "cases:case_settings",
            args=(case.pk,),
            query={"review_search": "1"},
            fragment="search-preparation",
        )
    )

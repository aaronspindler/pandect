"""Case-owner approval of external query and passage ranking."""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from config.access import product_login_required

from .search_settings import change_passage_ranking


@product_login_required
@require_POST
def change(request, case_id):
    outcome = change_passage_ranking(
        request.user,
        case_id,
        request.POST.get("action"),
        request.POST.get("confirmation", ""),
    )
    if outcome == "stale":
        messages.error(
            request, "Ranking settings changed. Review the current destination."
        )
    elif outcome == "approve":
        messages.success(request, "Hosted passage ranking approved for this case.")
    elif outcome == "revoke":
        messages.success(request, "Hosted passage ranking approval revoked.")
    return redirect(
        reverse("cases:case_settings", args=(case_id,), fragment="passage-ranking")
    )

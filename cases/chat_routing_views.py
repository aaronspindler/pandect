from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from config.access import product_login_required
from core.evidence_chat.decisions_approval import change_approval


@product_login_required
@require_POST
def change(request, case_id):
    outcome = change_approval(
        request.user,
        case_id,
        request.POST.get("action"),
        request.POST.get("confirmation", ""),
    )
    if outcome == "stale":
        messages.error(
            request, "Routing settings changed. Review the current destination."
        )
    elif outcome == "approve":
        messages.success(request, "OpenAI source selection approved for this case.")
    elif outcome == "revoke":
        messages.success(request, "OpenAI source selection approval revoked.")
    return redirect(
        reverse("cases:case_settings", args=(case_id,), fragment="chat-routing")
    )

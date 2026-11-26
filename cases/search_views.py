"""Case Settings endpoints for search preparation and destination approvals."""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from config.access import product_login_required
from core.evidence_chat.decisions_approval import change_approval

from .search_settings import (
    change_meaning_search,
    change_passage_ranking,
    change_preparation,
)


def _destination_approval(request, case_id, change, *, label, stale_message, fragment):
    outcome = change(
        request.user,
        case_id,
        request.POST.get("action"),
        request.POST.get("confirmation", ""),
    )
    if outcome == "stale":
        messages.error(request, stale_message)
    elif outcome == "approve":
        messages.success(request, f"{label} approved for this case.")
    elif outcome == "revoke":
        messages.success(request, f"{label} approval revoked.")
    return redirect(reverse("cases:case_settings", args=(case_id,), fragment=fragment))


@product_login_required
@require_POST
def chat_routing(request, case_id):
    return _destination_approval(
        request,
        case_id,
        change_approval,
        label="OpenAI source selection",
        stale_message="Routing settings changed. Review the current destination.",
        fragment="chat-routing",
    )


@product_login_required
@require_POST
def passage_ranking(request, case_id):
    return _destination_approval(
        request,
        case_id,
        change_passage_ranking,
        label="Hosted passage ranking",
        stale_message="Ranking settings changed. Review the current destination.",
        fragment="passage-ranking",
    )


@product_login_required
@require_POST
def meaning_search(request, case_id):
    outcome = change_meaning_search(
        request.user,
        case_id,
        request.POST.get("action"),
        request.POST.get("confirmation", ""),
    )
    if outcome == "stale":
        messages.error(
            request, "Settings changed. Review the meaning search destination."
        )
    elif outcome == "queue_failed":
        messages.error(
            request,
            "Indexing could not be queued. Review the destination and try again.",
        )
    elif outcome == "revoke":
        messages.success(request, "Meaning search approval revoked for this case.")
    elif outcome == "enable_chat":
        messages.success(
            request, "Hosted meaning search enabled for new chat questions."
        )
    elif outcome == "disable_chat":
        messages.success(request, "Hosted meaning search disabled for Evidence chat.")
    elif outcome == "approve":
        messages.success(request, "Meaning search approved. Indexing is queued.")
        return redirect(
            reverse(
                "core:analysis:processing",
                args=(case_id,),
                fragment="case-search-index",
            )
        )
    return redirect(
        reverse("cases:case_settings", args=(case_id,), fragment="meaning-search")
    )


@product_login_required
@require_POST
def search_preparation(request, case_id):
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

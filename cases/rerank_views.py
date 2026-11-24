"""Case-owner approval of external query and passage ranking."""

from uuid import uuid4

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_POST

from config.access import product_login_required
from core.analysis.execution import execution_confirmation
from core.case_search.rerank import destination, rerank_scope

from .permissions import get_case


def approval_context(case, actor):
    saved = case.search_rerank_approval
    route = destination()
    if route.get("provider") != "workers-ai":
        route = {}
    binding = [str(case.pk), str(actor.pk), saved.get("revision", "")]
    return {
        "destination": route,
        "saved": saved.get("destination", {}),
        "approved": bool(route and rerank_scope(case)),
        "approve": execution_confirmation(
            "case.search.rerank", [*binding, "approve"], route
        ),
        "revoke": execution_confirmation(
            "case.search.rerank", [*binding, "revoke"], {}
        ),
    }


@product_login_required
@require_POST
@transaction.atomic
def change(request, case_id):
    case = get_case(request.user, case_id, owner=True, lock=True)
    action = request.POST.get("action")
    if action not in {"approve", "revoke"}:
        raise Http404
    review = approval_context(case, request.user)
    if not constant_time_compare(request.POST.get("confirmation", ""), review[action]):
        messages.error(
            request, "Ranking settings changed. Review the current destination."
        )
    elif action == "approve" and review["destination"] and not review["approved"]:
        case.search_rerank_approval = {
            "destination": review["destination"],
            "revision": str(uuid4()),
            "approved_by": str(request.user.pk),
            "approved_at": timezone.now().isoformat(),
        }
        case.save(update_fields=("search_rerank_approval",))
        messages.success(request, "Hosted passage ranking approved for this case.")
    elif action == "revoke" and review["saved"]:
        case.search_rerank_approval = {"revision": str(uuid4())}
        case.save(update_fields=("search_rerank_approval",))
        messages.success(request, "Hosted passage ranking approval revoked.")
    return redirect(
        reverse("cases:case_settings", args=(case.pk,), fragment="passage-ranking")
    )

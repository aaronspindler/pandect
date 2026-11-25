"""Case-owner search destination approvals and preparation settings."""

from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.http import Http404
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from core.analysis.execution import execution_confirmation
from core.case_search.approval import (
    approve_case_search,
    revoke_case_search,
    search_approval_context,
    set_chat_queries,
)
from core.case_search.generation import (
    OPERATIONS,
    enqueue_generation,
    set_policy,
    settings_rows,
)
from core.case_search.indexing import prepare_case_search
from core.case_search.rerank import destination, rerank_scope

from .permissions import get_case


@transaction.atomic
def change_meaning_search(actor, case_id, action, confirmation):
    case = get_case(actor, case_id, owner=True, lock=True)
    if action not in {"approve", "revoke", "enable_chat", "disable_chat"}:
        raise Http404("Case action not found.")
    review = search_approval_context(case, actor)
    if not constant_time_compare(confirmation, review[f"{action}_confirmation"]):
        return "stale"
    if action == "revoke" and review["saved_destination"]:
        revoke_case_search(case)
        return "revoke"
    if (
        action == "enable_chat"
        and review["approved"]
        and not review["chat_queries_enabled"]
    ):
        set_chat_queries(case, enabled=True)
        return "enable_chat"
    if action == "disable_chat" and review["chat_queries_enabled"]:
        set_chat_queries(case, enabled=False)
        return "disable_chat"
    if action == "approve" and review["destination"] and not review["approved"]:
        try:
            with transaction.atomic():
                approve_case_search(case, actor, review)
                if not prepare_case_search(case, actor, retry=True):
                    raise ValidationError("The reviewed search configuration changed.")
        except DatabaseError, ValidationError:
            return "queue_failed"
        return "approve"
    return None


def rerank_approval_context(case, actor):
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


@transaction.atomic
def change_passage_ranking(actor, case_id, action, confirmation):
    case = get_case(actor, case_id, owner=True, lock=True)
    if action not in {"approve", "revoke"}:
        raise Http404
    review = rerank_approval_context(case, actor)
    if not constant_time_compare(confirmation, review[action]):
        return "stale"
    if action == "approve" and review["destination"] and not review["approved"]:
        case.search_rerank_approval = {
            "destination": review["destination"],
            "revision": str(uuid4()),
            "approved_by": str(actor.pk),
            "approved_at": timezone.now().isoformat(),
        }
        case.save(update_fields=("search_rerank_approval",))
        return "approve"
    if action == "revoke" and review["saved"]:
        case.search_rerank_approval = {"revision": str(uuid4())}
        case.save(update_fields=("search_rerank_approval",))
        return "revoke"
    return None


@transaction.atomic
def change_preparation(actor, case_id, feature, action, confirmation):
    case = get_case(actor, case_id, owner=True, lock=True)
    if feature not in OPERATIONS:
        raise Http404("Unknown search setting.")
    review = next(
        row for row in settings_rows(case, actor) if row["feature"] == feature
    )
    label = review["label"]
    if not constant_time_compare(confirmation, review["confirmation"]):
        return "stale", label
    if action == "enable" and review["ready"] and not review["enabled"]:
        set_policy(case, feature, True)
        return "enable", label
    if action == "check" and review["current"]:
        enqueue_generation(case, feature)
        return "check", label
    if action == "disable" and review["enabled"]:
        set_policy(case, feature, False)
        return "disable", label
    return "review_required", label

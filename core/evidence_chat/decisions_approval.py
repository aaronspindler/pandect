"""Owner consent for sending a bounded Evidence Chat shortlist to Decisions."""

from uuid import uuid4

from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.utils.crypto import constant_time_compare

from cases.permissions import get_case
from core.analysis.execution import execution_confirmation
from core.evidence_chat import decisions
from core.models import ChatRoutingApproval


def scope(case):
    """Return the currently authorized route and revision for a chat turn."""
    route = decisions.destination()
    if not route:
        return {}
    revision = (
        ChatRoutingApproval.objects.filter(
            case=case, changed_by_id=F("case__owner_id"), destination=route
        )
        .values_list("revision", flat=True)
        .first()
    )
    return route | {"revision": str(revision)} if revision else {}


def approval_context(case, actor):
    saved = ChatRoutingApproval.objects.filter(case=case).first()
    route = decisions.destination()
    binding = [str(case.pk), str(actor.pk), str(saved.revision) if saved else ""]
    return {
        "destination": route,
        "saved": saved.destination if saved else {},
        "approved": bool(
            route
            and saved
            and saved.changed_by_id == case.owner_id
            and saved.destination == route
        ),
        "approve": execution_confirmation("chat.routing", [*binding, "approve"], route),
        "revoke": execution_confirmation("chat.routing", [*binding, "revoke"], {}),
    }


@transaction.atomic
def change_approval(actor, case_id, action, confirmation):
    case = get_case(actor, case_id, owner=True, lock=True)
    if action not in {"approve", "revoke"}:
        raise Http404
    review = approval_context(case, actor)
    if not constant_time_compare(confirmation, review[action]):
        return "stale"
    if action == "approve" and review["destination"] and not review["approved"]:
        destination = review["destination"]
    elif action == "revoke" and review["saved"]:
        destination = {}
    else:
        return None
    # Keep the new revision after revocation so replaying an initial form cannot
    # reauthorize a transfer. Case row locking also serializes first approvals.
    ChatRoutingApproval.objects.update_or_create(
        case=case,
        defaults={"destination": destination, "changed_by": actor, "revision": uuid4()},
    )
    return action

"""Case-owner consent to send evidence passages and queries for embedding."""

from uuid import uuid4

from django.utils import timezone

from core.analysis.execution import execution_confirmation
from core.case_search import semantic_model
from core.models import CaseSearchIndex


def search_approval_current(case, *, revision=None):
    """Check a freshly authorized case against the current external destination."""
    grant = case.search_embedding_approval
    if not semantic_model.is_external() or not grant.get("profile"):
        return False
    return bool(
        grant.get("approved_by") == str(case.owner_id)
        and grant.get("revision")
        and (revision is None or grant["revision"] == revision)
        and grant["profile"] == semantic_model.profile()
    )


def search_approval_context(case, actor):
    grant = case.search_embedding_approval
    external = semantic_model.is_external()
    profile = semantic_model.profile() if external else ""
    destination = semantic_model.identity(semantic_model.execution()) if profile else {}
    scope = [str(case.pk), str(actor.pk), grant.get("revision", "")]
    return {
        "visible": bool(external or grant.get("profile")),
        "approved": search_approval_current(case),
        "saved_destination": grant.get("execution", {}),
        "profile": profile,
        "destination": destination,
        "approve_confirmation": execution_confirmation(
            "case.search.embedding", [*scope, "approve"], [profile, destination]
        ),
        "revoke_confirmation": execution_confirmation(
            "case.search.embedding", [*scope, "revoke"], {}
        ),
    }


def approve_case_search(case, owner, review):
    """Record the destination reviewed by the locked, verified case owner."""
    case.search_embedding_approval = {
        "approved_by": str(owner.pk),
        "approved_at": timezone.now().isoformat(),
        "revision": str(uuid4()),
        "profile": review["profile"],
        "execution": review["destination"],
    }
    case.save(update_fields=("search_embedding_approval",))


def revoke_case_search(case):
    """Invalidate pending deliveries in the same transaction as the grant."""
    profile = case.search_embedding_approval["profile"]
    case.search_embedding_approval = {"revision": str(uuid4())}
    case.save(update_fields=("search_embedding_approval",))
    CaseSearchIndex.objects.filter(case=case, profile=profile).update(
        status="failed", error_code="embedding_approval_required", token=uuid4()
    )

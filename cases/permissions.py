"""Current case authorization shared by investigator-facing entry points."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F, FilteredRelation, Q
from django.http import Http404

from .models import Case, CaseMembership


def accessible_cases(user):
    if not user.is_authenticated or not user.is_active:
        return Case.objects.none()
    owned_ids = Case.objects.filter(owner=user).values("pk")
    shared_ids = CaseMembership.objects.filter(user=user).values("case_id")
    return Case.objects.filter(pk__in=owned_ids.union(shared_ids, all=True))


def case_role(case, user):
    if case.owner_id == user.pk:
        return "owner"
    return (
        CaseMembership.objects.filter(case=case, user=user)
        .values_list("role", flat=True)
        .first()
    )


def get_case(user, case_id, *, edit=False, owner=False, lock=False):
    case, _ = get_case_with_role(user, case_id, edit=edit, owner=owner, lock=lock)
    return case


def get_case_with_role(user, case_id, *, edit=False, owner=False, lock=False):
    """Lock only inside a transaction; inaccessible UUIDs remain undisclosed."""
    if not user.is_authenticated or not user.is_active:
        raise Http404("Case not found.")
    queryset = Case.objects.alias(
        member=FilteredRelation("memberships", condition=Q(memberships__user=user))
    ).filter(Q(owner=user) | Q(member__isnull=False))
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    else:
        queryset = queryset.annotate(member_role=F("member__role"))
    try:
        case = queryset.select_related("owner", "activity").get(pk=case_id)
    except (Case.DoesNotExist, ValidationError, ValueError) as exc:
        raise Http404("Case not found.") from exc
    if lock:
        # Recheck membership after acquiring the case lock, which may have waited
        # for a concurrent access change.
        role = case_role(case, user)
    else:
        role = "owner" if case.owner_id == user.pk else case.member_role
    if not role:
        raise Http404("Case not found.")
    if owner and role != "owner":
        raise PermissionDenied("Only the case owner can do this.")
    if edit and role not in ("owner", "editor"):
        raise PermissionDenied("Your Viewer access does not allow changes.")
    return case, role


def case_context(case, user):
    role = case_role(case, user)
    return {
        "case": case,
        "case_activity_versions": case.activity.versions(),
        "case_role": role,
        "can_edit": role in ("owner", "editor"),
        "is_owner": role == "owner",
    }

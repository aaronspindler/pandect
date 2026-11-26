"""List, open, create, and delete investigations."""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case as CaseExpression
from django.db.models import Count, OuterRef, Q, Subquery, Value, When
from django.db.models.deletion import ProtectedError, RestrictedError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from config.access import product_login_required
from core.evidence.types import KINDS
from core.models import EvidenceChat, ImportantItem, ImportantNote
from geo.models import PointOfInterest
from socialmedia.models import SocialAccount
from ui.live import render_live

from .deletion import CASCADE_CONFLICT, CaseDeletionBlocked, delete_case_record
from .forms import (
    CaseForm,
    ConfirmForm,
)
from .models import CaseMembership
from .permissions import accessible_cases, get_case
from .responses import render_case


def _case_record_counts(case):
    return {
        "people_count": case.people.count(),
        "vehicles_count": case.vehicles.count(),
        "collaborator_count": case.memberships.count(),
        "pending_invitation_count": case.invitations.filter(
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).count(),
    }


def _archive_counts(queryset):
    return queryset.aggregate(
        active=Count("pk", filter=Q(is_archived=False)),
        archived=Count("pk", filter=Q(is_archived=True)),
    )


@product_login_required
@require_http_methods(["GET"])
def case_list(request):
    query = request.GET.get("q", "")[:200].strip()
    ownership = request.GET.get("ownership", "all")
    membership_role = CaseMembership.objects.filter(
        case_id=OuterRef("pk"), user=request.user
    ).values("role")[:1]
    queryset = (
        accessible_cases(request.user)
        .select_related("owner")
        .annotate(
            current_role=CaseExpression(
                When(owner=request.user, then=Value("owner")),
                default=Subquery(membership_role),
            )
        )
    )
    if query:
        queryset = queryset.filter(
            Q(name__icontains=query) | Q(description__icontains=query)
        )
    if ownership == "owned":
        queryset = queryset.filter(owner=request.user)
    elif ownership == "shared":
        queryset = queryset.exclude(owner=request.user)
    else:
        ownership = "all"
    page = Paginator(
        queryset.order_by("-updated_at", "-created_at", "pk"), 25
    ).get_page(request.GET.get("page"))
    return render_live(
        request,
        "cases/list.html",
        {"page_obj": page, "query": query, "ownership": ownership},
        collection=True,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def case_create(request):
    form = CaseForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        case = form.save(commit=False)
        case.owner = request.user
        case.save()
        messages.success(
            request, "Case created. Add evidence to begin your investigation."
        )
        return redirect("core:analysis:index", case_id=case.pk)
    return render(request, "cases/form.html", {"form": form, "creating": True})


@product_login_required
@require_http_methods(["GET"])
def case_detail(request, case_id):
    case = get_case(request.user, case_id)
    return redirect("core:analysis:index", case_id=case.pk)


@product_login_required
@require_http_methods(["GET", "POST"])
def case_delete(request, case_id):
    form = ConfirmForm(request.POST if request.method == "POST" else None)
    status = 200
    if request.method == "POST":
        with transaction.atomic():
            case = get_case(request.user, case_id, owner=True, lock=True)
            if form.is_valid():
                try:
                    delete_case_record(case, case.pk)
                except CaseDeletionBlocked, ProtectedError, RestrictedError:
                    form.add_error(None, CASCADE_CONFLICT)
                    status = 409
                else:
                    messages.success(
                        request, "Case deleted. Your account is unchanged."
                    )
                    return redirect("cases:case_list")
    else:
        case = get_case(request.user, case_id, owner=True)
    evidence_counts = [
        _archive_counts(model.objects.filter(case=case)) for model in KINDS.values()
    ]
    counts = {
        **_case_record_counts(case),
        "social_account_count": SocialAccount.objects.filter(person__case=case).count(),
        "instagram_monitor_count": case.instagram_monitors.count(),
        "poi_count": PointOfInterest.objects.filter(person__case=case).count(),
        "active_evidence_count": sum(item["active"] for item in evidence_counts),
        "archived_evidence_count": sum(item["archived"] for item in evidence_counts),
        "chat_count": EvidenceChat.objects.filter(case=case).count(),
        "important_item_count": ImportantItem.objects.filter(case=case).count(),
        "important_note_count": ImportantNote.objects.filter(item__case=case).count(),
    }
    return render_case(
        request, "cases/delete.html", case, form=form, status=status, **counts
    )

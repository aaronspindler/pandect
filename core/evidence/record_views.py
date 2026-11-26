"""Edit evidence details and manage archive, restore, and reviewed deletion."""

from django.contrib import messages
from django.contrib.admin.models import DELETION, LogEntry
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.evidence.deletion import (
    CHANGED,
    deletion_manifest_response,
    deletion_review_context,
    execute_deletion,
    get_deletion_target,
    load_deletion_review,
    plan_deletion,
    save_deletion_review,
)
from core.evidence.form_registry import TYPE_FIELDS, evidence_form
from core.evidence.forms import COMMON_FIELDS, EvidenceDeleteForm
from core.evidence.navigation import list_params, query_suffix
from core.evidence.queries import get_evidence
from core.evidence.responses import (
    detail_url,
)
from core.evidence.retention import (
    set_evidence_archive,
)
from core.evidence.types import (
    KIND_LABELS,
    READER_TEMPLATES,
)
from ui.dialogs import form_saved, render_quick_form


def _info_url(request, case, kind, item):
    params = list_params(request)
    params["info"] = "1"
    return (
        detail_url(case, kind, item)
        + "?"
        + params.urlencode()
        + "#evidence-details-heading"
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def edit(request, case_id, kind, evidence_id):
    is_post = request.method == "POST"
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=is_post)
        item = get_evidence(case, kind, evidence_id, lock=is_post)
        if item.is_archived:
            raise Http404("Restore this evidence before editing it.")
        saved_timestamp = item.edited_at
        latest_details = [
            (item._meta.get_field(name).verbose_name.capitalize(), getattr(item, name))
            for name in COMMON_FIELDS + TYPE_FIELDS[kind]
        ]
        conflict = False
        form = evidence_form(kind, editing=True)(
            request.POST if is_post else None, kind=kind, instance=item
        )
        status = 200
        if is_post and form.is_valid():
            if form.cleaned_data["expected_edited_at"] != saved_timestamp:
                form.add_error(
                    None,
                    "These details changed while you were editing. "
                    "Review the latest saved details before trying again.",
                )
                form.data = form.data.copy()
                form.data["expected_edited_at"] = saved_timestamp.isoformat()
                conflict = True
                status = 409
            else:
                form.save()
                return form_saved(
                    request,
                    "Evidence details saved. The original is unchanged.",
                    _info_url(request, case, kind, item),
                )
        elif is_post:
            status = 400
        context = case_context(case, request.user) | {
            "form": form,
            "item": item,
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
            "reader_template": READER_TEMPLATES[kind],
            "editing": True,
            "nav_section": "evidence",
            "list_suffix": query_suffix(request),
            "cancel_url": _info_url(request, case, kind, item),
            "conflict": conflict,
            "latest_details": latest_details if conflict else [],
        }
    return render_quick_form(request, "core/evidence/form.html", context, status=status)


@product_login_required
@require_POST
def archive(request, case_id, kind, evidence_id):
    return _set_archive(request, case_id, kind, evidence_id, True)


@product_login_required
@require_POST
def restore(request, case_id, kind, evidence_id):
    return _set_archive(request, case_id, kind, evidence_id, False)


def _set_archive(request, case_id, kind, evidence_id, archived):
    case, item = set_evidence_archive(
        request.user, case_id, kind, evidence_id, archived=archived
    )
    messages.success(
        request,
        "Evidence archived. Its original is retained."
        if archived
        else "Evidence restored to the active collection.",
    )
    return redirect(_info_url(request, case, kind, item))


@product_login_required
@require_http_methods(["GET", "POST"])
def delete(request, case_id, kind, evidence_id):
    status = 200 if request.method == "GET" else 400
    with transaction.atomic():
        case, item = get_deletion_target(
            request.user, case_id, kind, evidence_id, lock=request.method == "POST"
        )
        cached = (
            load_deletion_review(request, case, item, kind)
            if request.method == "GET" and not request.GET.get("download")
            else None
        )
        plan = cached or plan_deletion(item, kind)
        if (
            request.method == "GET"
            and request.GET.get("download") == "manifest"
            and not plan.blocker
            and (
                not request.GET.get("review_revision")
                or plan.matches(request.GET["review_revision"], request.user)
            )
        ):
            return deletion_manifest_response(plan)
        if request.method == "GET" and cached is None:
            save_deletion_review(case, request.user, plan)
        form = EvidenceDeleteForm(
            request.POST if request.method == "POST" else None,
            initial={"revision": plan.revision(request.user)},
        )
        if request.method == "POST" and not plan.blocker and form.is_valid():
            if not plan.matches(form.cleaned_data["revision"], request.user):
                form.add_error(None, CHANGED)
                status = 409
            else:
                LogEntry.objects.log_actions(request.user.pk, [item], DELETION)
                execute_deletion(plan, request.user)
                messages.success(request, "Evidence deleted.")
                return redirect(
                    reverse("core:evidence:list", kwargs={"case_id": case.pk})
                    + query_suffix(request)
                )
        if request.method == "POST":
            # A refreshed inventory always requires another deliberate check.
            form.data = form.data.copy()
            form.data["revision"] = plan.revision(request.user)
            form.data.pop("confirm", None)
            if plan.blocker:
                status = 409
    review_context = deletion_review_context(request, plan)
    if review_context["inventory_changed"]:
        status = 409
    return render(
        request,
        "core/evidence/delete.html",
        {
            **case_context(case, request.user),
            "item": item,
            "form": form,
            "deletion_blocker": plan.blocker,
            "analysis_count": plan.analysis_count,
            **review_context,
            "cancel_url": _info_url(request, case, kind, item),
            "nav_section": "evidence",
        },
        status=status,
    )

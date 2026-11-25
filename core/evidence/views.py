"""Case-authorized investigator workflows for retained original evidence."""

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.models import DELETION, LogEntry
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)
from django.views.decorators.vary import vary_on_headers

from cases.permissions import case_context, get_case
from config.access import product_login_required, wants_json
from core.analysis.errors import AnalysisError
from core.analysis.findings import evidence_findings_context
from core.evidence.collection import collection_context
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
from core.evidence.forms import COMMON_FIELDS, SOURCE_FIELDS, EvidenceDeleteForm
from core.evidence.navigation import list_params, query_suffix
from core.evidence.queries import evidence_counts_by_type, get_evidence
from core.evidence.responses import (
    SAVE_UNCONFIRMED,
    detail_url,
    error_response,
    file_download,
)
from core.evidence.retention import (
    SubmissionConflict,
    SubmissionUnavailable,
    set_evidence_archive,
    submit_evidence,
)
from core.evidence.types import (
    CALL_KINDS,
    COMMUNICATION_KINDS,
    FILE_KINDS,
    KIND_LABELS,
    KINDS,
    LOCATION_KINDS,
    READER_TEMPLATES,
)
from core.important.reader import reader_context as important_reader_context
from geo.models import VehicleLocationImport
from image.previews import image_preview
from ui.dialogs import form_saved, render_quick_form, render_record, wants_record
from ui.live import render_live

EVIDENCE_SUMMARY_ICONS = {
    "image": "photo",
    "audio": "music",
    "document": "file-text",
    "text": "file-text",
    "location": "map",
    "person-location": "users",
    "vehicle-location": "car",
    "text-message": "message",
    "phone-call": "phone",
    "facetime": "phone",
}


def _info_url(request, case, kind, item):
    params = list_params(request)
    params["info"] = "1"
    return (
        detail_url(case, kind, item)
        + "?"
        + params.urlencode()
        + "#evidence-details-heading"
    )


def _list_context(request, case, *, workspace_rows=True):
    context = collection_context(case, request.GET, workspace_rows=workspace_rows)
    if workspace_rows:
        for row in context["rows"]:
            row["icon"] = EVIDENCE_SUMMARY_ICONS.get(row["kind"], "file-text")
    return context


@product_login_required
@require_safe
def evidence_list(request, case_id):
    case = get_case(request.user, case_id)
    gallery = (
        request.GET.get("type") == "image" and request.GET.get("view") == "gallery"
    )
    context = case_context(case, request.user) | _list_context(
        request, case, workspace_rows=not gallery
    )
    context["nav_section"] = "evidence"
    if gallery:
        return render_live(request, "image/gallery.html", context, collection=True)
    state = context["state"]
    counts = evidence_counts_by_type(
        case, archived=None if state == "all" else state == "archived"
    )
    context["evidence_summary_heading"] = {
        "active": "Active evidence by type",
        "archived": "Archived evidence by type",
        "all": "All evidence by type",
    }[state]
    context["evidence_summary"] = [
        {
            "label": label,
            "count": counts[kind],
            "icon": EVIDENCE_SUMMARY_ICONS.get(kind),
        }
        for kind, label in KIND_LABELS.items()
    ]
    return render_live(
        request, "core/evidence/workspace.html", context, collection=True
    )


@product_login_required
@require_safe
@vary_on_headers("X-Pandect-Form")
def detail(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    item = get_evidence(
        case,
        kind,
        evidence_id,
        identity_only=request.headers.get("X-Pandect-Live") in {"1", "collection"},
    )
    return render_detail(request, case, kind, item)


def render_detail(request, case, kind, item, *, analysis_form=None, status=200):
    """Render the complete reader for an already-authorized case and original."""
    context = case_context(case, request.user) | {"item": item, "kind": kind}
    mode = (
        request.headers.get("X-Pandect-Live")
        if request.method in {"GET", "HEAD"}
        else None
    )
    record_only = wants_record(request) and mode is None
    if mode != "1" and not record_only:
        context.update(_list_context(request, case))
    if mode == "collection":
        return render_live(
            request, "core/evidence/workspace.html", context, collection=True
        )
    if not record_only:
        context.update(
            evidence_findings_context(case, kind, item, request.GET, form=analysis_form)
        )
    if mode == "1":
        context["list_suffix"] = query_suffix(request)
        return render_live(request, "core/evidence/workspace.html", context)
    technical = [
        (item._meta.get_field(name).verbose_name.capitalize(), getattr(item, name))
        for name in TYPE_FIELDS[kind]
        if getattr(item, name) not in (None, "")
    ]
    context.update(
        {
            "kind_label": KIND_LABELS[kind],
            "reader_template": READER_TEMPLATES[kind],
            "technical_details": technical,
            "nav_section": "evidence",
            "is_location": kind in LOCATION_KINDS,
            "is_communication": kind in COMMUNICATION_KINDS,
            "is_call": kind in CALL_KINDS,
            "can_delete_evidence": request.user.is_superuser,
            "info_open": request.GET.get("info") == "1"
            or request.GET.get("section") == "details"
            or any(key in request.GET for key in ("thread_page", "reply_page")),
        }
    )
    if record_only:
        if kind == "text-message":
            from phone.presentation import message_evidence_label

            context["display_title"] = message_evidence_label(
                item.body, item.sender, item.occurred_at
            )
        return render_record(request, "core/evidence/workspace.html", context)
    reader_params = list_params(request)
    for key in (
        "findings_page",
        "results_page",
        "thread_page",
        "reply_page",
        "conversation_list_page",
    ):
        page = request.GET.get(key, "")
        if len(page) <= 9 and page.isascii() and page.isdecimal():
            reader_params[key] = page
    if context["info_open"]:
        reader_params["info"] = "1"
    context.update(
        important_reader_context(
            request,
            case,
            evidence_kind=kind,
            evidence_id=item.pk,
            source_url=reverse(
                "core:evidence:detail",
                args=[case.pk, kind, item.pk],
                query=reader_params,
                fragment="evidence-reader",
            ),
        )
    )
    if kind in COMMUNICATION_KINDS:
        from phone.conversations import conversation_list_page
        from phone.presentation import reader_context

        context.update(
            reader_context(
                case,
                kind,
                item,
                conversation_list_page=conversation_list_page(
                    request.GET.get("conversation_list_page")
                ),
                native_thread_page=request.GET.get("thread_page"),
                native_reply_page=request.GET.get("reply_page"),
            )
        )
    if kind == "document":
        from phone.models import CallHistoryImport

        context["vehicle_import"] = VehicleLocationImport.objects.filter(
            case=case, document=item
        ).first()
        context["call_history_csv"] = item.original_filename.lower().endswith(".csv")
        if context["call_history_csv"]:
            context["call_history_import"] = (
                CallHistoryImport.objects.filter(
                    case=case,
                    document=item,
                    source__case=case,
                    source_sha256=item.sha256,
                )
                .order_by("-created_at", "-pk")
                .first()
            )
    elif kind == "vehicle-location":
        from geo.trip_processing import observation_trip_url

        context["observation_trip_url"] = observation_trip_url(case, item)
    return render_live(request, "core/evidence/workspace.html", context, status=status)


def _created_response(request, case, kind, item, *, created):
    url = detail_url(case, kind, item)
    if wants_json(request):
        return JsonResponse(
            {"type": kind, "id": str(item.pk), "detail_url": url},
            status=201 if created else 200,
        )
    messages.success(
        request, "Evidence added." if created else "This evidence was already saved."
    )
    return redirect(url)


def _duplicate_context(case, item, review):
    matches = [
        match
        | {
            "id": str(match["id"]),
            "label": KIND_LABELS[match["kind"]],
            "url": reverse(
                "core:evidence:detail", args=(case.pk, match["kind"], match["id"])
            ),
        }
        for match in review.matches[:10]
    ]
    return {
        "matches": matches,
        "has_more": len(review.matches) > 10,
        "all_matches_url": reverse(
            "core:evidence:list", args=(case.pk,), query={"checksum": item.sha256}
        ),
        "confirmation": review.confirmation,
    }


def _add_files(request, case):
    form = evidence_form("document")(
        kind="document", instance=KINDS["document"](case=case, uploaded_by=request.user)
    )
    context = case_context(case, request.user) | {
        "shared_fields": [form[name] for name in SOURCE_FIELDS],
        "max_batch_files": settings.PANDECT_MAX_BATCH_FILES,
        "max_file_bytes": settings.PANDECT_MAX_FILE_BYTES,
        "manual_kinds": [
            (kind, label)
            for kind, label in KIND_LABELS.items()
            if kind not in (*FILE_KINDS, "text")
        ],
        "nav_section": "evidence",
    }
    return render(request, "core/evidence/bulk_form.html", context)


@product_login_required
@require_safe
def bulk_create(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    return redirect("core:evidence:create", case_id=case.pk)


@product_login_required
@require_http_methods(["GET", "POST"])
def create(request, case_id):
    try:
        case = get_case(request.user, case_id, edit=True)
    except PermissionDenied:
        if wants_json(request):
            return error_response(
                request,
                "permission_denied",
                "You no longer have permission to add evidence.",
                403,
            )
        raise
    except Http404:
        if wants_json(request):
            return error_response(
                request, "not_found", "This case is unavailable.", 404
            )
        raise
    if request.method == "GET" and "kind" not in request.GET:
        return _add_files(request, case)
    kind = (
        request.POST.get("kind", "document")
        if request.method == "POST"
        else request.GET.get("kind", "document")
    )
    if kind not in KINDS:
        return error_response(
            request, "invalid_type", "Choose a supported evidence type."
        )
    status = 200
    duplicates = None
    if request.method == "POST":
        if getattr(request, "evidence_upload_too_large", False):
            return error_response(
                request,
                "file_too_large",
                "The file exceeds the configured upload limit.",
                413,
            )
        if sum(len(request.FILES.getlist(key)) for key in request.FILES) > 1:
            return error_response(request, "too_many_files", "Send one file at a time.")
        try:
            submission = submit_evidence(
                request.user, case_id, kind, request.POST, request.FILES
            )
        except PermissionDenied:
            if not wants_json(request):
                raise
            return error_response(
                request,
                "permission_denied",
                "You no longer have permission to add evidence.",
                403,
            )
        except Http404:
            if not wants_json(request):
                raise
            return error_response(
                request, "not_found", "This case is unavailable.", 404
            )
        except SubmissionConflict as exc:
            return error_response(request, "submission_conflict", str(exc), 409)
        except SubmissionUnavailable as exc:
            code, message = (
                (
                    "storage_unavailable",
                    "The original could not be saved. Keep your file and try again.",
                )
                if exc.storage
                else ("save_unconfirmed", SAVE_UNCONFIRMED)
            )
            if wants_json(request) or exc.form is None:
                return error_response(request, code, message, 503)
            form = exc.form
            case = form.instance.case
            form.add_error(None, message)
            status = 503
        else:
            form = submission.form
            case = form.instance.case
            if submission.item is not None:
                return _created_response(
                    request, case, kind, submission.item, created=submission.created
                )
            if submission.duplicates:
                duplicates = _duplicate_context(
                    case, form.instance, submission.duplicates
                )
                if wants_json(request):
                    return JsonResponse(
                        {"code": "duplicate_evidence", **duplicates}, status=409
                    )
                status = 409
            elif wants_json(request):
                upload = request.FILES.get("file")
                status = (
                    413
                    if "file" in form.errors
                    and upload
                    and upload.size > settings.PANDECT_MAX_FILE_BYTES
                    else 400
                )
                return error_response(
                    request,
                    "validation_error",
                    "Review the highlighted fields.",
                    status,
                    form.errors.get_json_data(),
                )
            else:
                status = 400
    else:
        form = evidence_form(kind)(
            kind=kind,
            instance=KINDS[kind](case=case, uploaded_by=request.user),
            initial={"vehicle": request.GET.get("vehicle")}
            if kind == "vehicle-location"
            else None,
        )
    context = case_context(case, request.user) | {
        "form": form,
        "duplicates": duplicates,
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "reader_template": READER_TEMPLATES[kind],
        "kind_choices": KIND_LABELS.items(),
        "is_file": kind in FILE_KINDS,
        "is_communication": kind in COMMUNICATION_KINDS,
        "is_call": kind in CALL_KINDS,
        "has_people": (
            form.fields["person"].queryset.exists()
            if kind == "person-location"
            else kind not in COMMUNICATION_KINDS
            or form.fields["sender"].queryset.exists()
        ),
        "has_vehicles": kind != "vehicle-location"
        or form.fields["vehicle"].queryset.exists(),
        "nav_section": "evidence",
    }
    return render(request, "core/evidence/form.html", context, status=status)


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
@require_safe
def preview(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    if kind != "image":
        raise Http404
    item = get_evidence(case, kind, evidence_id)
    try:
        content = image_preview(item, thumbnail=request.GET.get("size") == "thumbnail")
    except AnalysisError, OSError, ValueError:
        response = HttpResponse(
            "Image preview unavailable. Download the original to inspect it.",
            status=422,
            content_type="text/plain",
        )
    else:
        response = HttpResponse(content, content_type="image/jpeg")
        response["Content-Disposition"] = f'inline; filename="preview-{item.pk}.jpg"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@product_login_required
@require_safe
def download(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    item = get_evidence(case, kind, evidence_id)
    # Linux WebKit can render typed attachments inline; use a download MIME type.
    if kind == "text":
        response = HttpResponse(
            item.body.encode("utf-8"), content_type="application/octet-stream"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="evidence-{item.pk}.txt"'
        )
    elif kind in (*LOCATION_KINDS, *COMMUNICATION_KINDS):
        response = HttpResponse(
            item.original_bytes(), content_type="application/octet-stream"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="evidence-{item.pk}.json"'
        )
    else:
        response = file_download(request, case, kind, item)
    response["X-Content-Type-Options"] = "nosniff"
    return response


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

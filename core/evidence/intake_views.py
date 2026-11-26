"""Accept evidence submissions and present duplicate review and recovery."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import (
    require_http_methods,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required, wants_json
from core.evidence.form_registry import evidence_form
from core.evidence.forms import SOURCE_FIELDS
from core.evidence.responses import (
    SAVE_UNCONFIRMED,
    detail_url,
    error_response,
)
from core.evidence.retention import (
    SubmissionConflict,
    SubmissionUnavailable,
    submit_evidence,
)
from core.evidence.types import (
    CALL_KINDS,
    COMMUNICATION_KINDS,
    FILE_KINDS,
    KIND_LABELS,
    KINDS,
    READER_TEMPLATES,
)


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

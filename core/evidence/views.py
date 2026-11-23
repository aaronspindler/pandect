"""Case-authorized investigator workflows for retained original evidence."""

from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.models import DELETION, LogEntry
from django.core import signing
from django.core.exceptions import (
    PermissionDenied,
    SuspiciousFileOperation,
    ValidationError,
)
from django.db import DatabaseError, IntegrityError, transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import get_valid_filename
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required, wants_json
from core.analysis.automatic import start_added_evidence_analysis
from core.analysis.errors import AnalysisError
from core.analysis.findings import evidence_findings_context
from core.evidence.collection import collection_context
from core.evidence.deletion import (
    CHANGED,
    deletion_manifest_response,
    deletion_review_context,
    execute_deletion,
    get_deletion_target,
    plan_deletion,
)
from core.evidence.form_registry import TYPE_FIELDS, evidence_form
from core.evidence.forms import COMMON_FIELDS, SOURCE_FIELDS, EvidenceDeleteForm
from core.evidence.navigation import list_params, query_suffix
from core.evidence.queries import duplicate_matches, get_evidence
from core.evidence.types import (
    CALL_KINDS,
    COMMUNICATION_KINDS,
    FILE_KINDS,
    KIND_LABELS,
    KINDS,
    LOCATION_KINDS,
    READER_TEMPLATES,
)
from core.storage import retain_uploaded_originals
from geo.models import VehicleLocationImport
from image.previews import image_preview
from phone.forms import save_call_content
from ui.live import render_live

SAVE_UNCONFIRMED = (
    "The save could not be confirmed. Keep your original and retry this submission."
)


def _error(request, code, message, status=400, errors=None):
    if wants_json(request):
        return JsonResponse(
            {"code": code, "message": message, "errors": errors or {}},
            status=status,
        )
    return render(
        request,
        "core/evidence/error.html",
        {"error_message": message},
        status=status,
    )


def _detail_url(case, kind, item):
    return reverse(
        "core:evidence:detail",
        kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
    )


def _info_url(request, case, kind, item):
    params = list_params(request)
    params["info"] = "1"
    return (
        _detail_url(case, kind, item)
        + "?"
        + params.urlencode()
        + "#evidence-details-heading"
    )


def _list_context(request, case):
    return collection_context(case, request.GET)


@product_login_required
@require_safe
def evidence_list(request, case_id):
    case = get_case(request.user, case_id)
    context = case_context(case, request.user) | _list_context(request, case)
    context["nav_section"] = "evidence"
    if context["selected_kind"] == "image" and request.GET.get("view") == "gallery":
        return render_live(request, "image/gallery.html", context, collection=True)
    return render_live(
        request, "core/evidence/workspace.html", context, collection=True
    )


@product_login_required
@require_safe
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
    if mode != "1":
        context.update(_list_context(request, case))
    if mode == "collection":
        return render_live(
            request, "core/evidence/workspace.html", context, collection=True
        )
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
        context["vehicle_import"] = VehicleLocationImport.objects.filter(
            case=case, document=item
        ).first()
    elif kind == "vehicle-location":
        from geo.trip_processing import observation_trip_url

        context["observation_trip_url"] = observation_trip_url(case, item)
    return render_live(request, "core/evidence/workspace.html", context, status=status)


def _created_response(request, case, kind, item, *, created):
    url = _detail_url(case, kind, item)
    if wants_json(request):
        return JsonResponse(
            {"type": kind, "id": str(item.pk), "detail_url": url},
            status=201 if created else 200,
        )
    messages.success(
        request, "Evidence added." if created else "This evidence was already saved."
    )
    return redirect(url)


def _duplicate_review(request, case, kind, item):
    if kind not in (*FILE_KINDS, "text"):
        return None
    identity = [str(case.pk), str(request.user.pk), kind, item.sha256]
    confirmation = request.POST.get("duplicate_confirmation", "")
    try:
        if signing.loads(confirmation, salt="core.evidence.duplicate") == identity:
            return None
    except signing.BadSignature:
        pass
    matches = list(duplicate_matches(case, item.sha256)[:11])
    if not matches:
        return None
    for match in matches[:10]:
        match["id"] = str(match["id"])
        match["label"] = KIND_LABELS[match["kind"]]
        match["url"] = reverse(
            "core:evidence:detail",
            kwargs={
                "case_id": case.pk,
                "kind": match["kind"],
                "evidence_id": match["id"],
            },
        )
    return {
        "matches": matches[:10],
        "has_more": len(matches) > 10,
        "all_matches_url": reverse("core:evidence:list", args=(case.pk,))
        + "?state=all&checksum="
        + item.sha256,
        "confirmation": signing.dumps(identity, salt="core.evidence.duplicate"),
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
            return _error(
                request,
                "permission_denied",
                "You no longer have permission to add evidence.",
                403,
            )
        raise
    except Http404:
        if wants_json(request):
            return _error(request, "not_found", "This case is unavailable.", 404)
        raise
    if request.method == "GET" and "kind" not in request.GET:
        return _add_files(request, case)
    kind = (
        request.POST.get("kind", "document")
        if request.method == "POST"
        else request.GET.get("kind", "document")
    )
    if kind not in KINDS:
        return _error(request, "invalid_type", "Choose a supported evidence type.")
    form_class = evidence_form(kind)
    form = form_class(
        kind=kind,
        instance=KINDS[kind](case=case, uploaded_by=request.user),
        initial={"vehicle": request.GET.get("vehicle")}
        if kind == "vehicle-location"
        else None,
    )
    status = 200
    duplicates = None
    if request.method == "POST":
        if getattr(request, "evidence_upload_too_large", False):
            return _error(
                request,
                "file_too_large",
                "The file exceeds the configured upload limit.",
                413,
            )
        if sum(len(request.FILES.getlist(key)) for key in request.FILES) > 1:
            return _error(request, "too_many_files", "Send one file at a time.")
        try:
            submission_id = UUID(request.POST.get("submission_id", ""))
        except ValueError, AttributeError:
            submission_id = None
        item = None
        try:
            with retain_uploaded_originals() as originals:
                case = get_case(request.user, case_id, edit=True, lock=True)
                if submission_id:
                    previous = (
                        KINDS[kind].objects.filter(submission_id=submission_id).first()
                    )
                    if previous:
                        if (
                            previous.case_id != case.pk
                            or previous.uploaded_by_id != request.user.pk
                        ):
                            return _error(
                                request,
                                "submission_conflict",
                                "This submission cannot be reused.",
                                409,
                            )
                        return _created_response(
                            request, case, kind, previous, created=False
                        )
                instance = KINDS[kind](
                    case=case, uploaded_by=request.user, submission_id=submission_id
                )
                form = form_class(
                    request.POST, request.FILES, kind=kind, instance=instance
                )
                if form.is_valid():
                    duplicates = _duplicate_review(request, case, kind, form.instance)
                    if duplicates:
                        if wants_json(request):
                            return JsonResponse(
                                {"code": "duplicate_evidence", **duplicates},
                                status=409,
                            )
                        status = 409
                    else:
                        item = form.save()
                        if kind == "vehicle-location":
                            from geo.trip_processing import invalidate_vehicle_trips

                            invalidate_vehicle_trips(
                                request.user, case, [item.vehicle_id]
                            )
                        original = getattr(item, "file", None)
                        if original:
                            originals.append(original)
                        attachment = None
                        if kind in CALL_KINDS:
                            attachment = save_call_content(
                                item, request.user, form.cleaned_data, submission_id
                            )
                            if attachment and attachment.file:
                                originals.append(attachment.file)
                        start_added_evidence_analysis(
                            request.user,
                            case.pk,
                            kind,
                            item.pk,
                            attachment_id=attachment.pk if attachment else None,
                            new_call=kind in CALL_KINDS,
                        )
                else:
                    status = 400
        except PermissionDenied:
            if not wants_json(request):
                raise
            return _error(
                request,
                "permission_denied",
                "You no longer have permission to add evidence.",
                403,
            )
        except Http404:
            if not wants_json(request):
                raise
            return _error(request, "not_found", "This case is unavailable.", 404)
        except OSError:
            message = "The original could not be saved. Keep your file and try again."
            if wants_json(request):
                return _error(request, "storage_unavailable", message, 503)
            form.add_error(None, message)
            status = 503
        except IntegrityError:
            return _error(
                request,
                "submission_conflict",
                "This submission could not be confirmed. "
                "Retry with the same submission ID.",
                409,
            )
        except AnalysisError, ValidationError, DatabaseError:
            if wants_json(request) or not form.is_bound:
                return _error(request, "save_unconfirmed", SAVE_UNCONFIRMED, 503)
            form.add_error(None, SAVE_UNCONFIRMED)
            status = 503
        else:
            if item is not None:
                return _created_response(request, case, kind, item, created=True)
        if wants_json(request):
            status = (
                413
                if "file" in form.errors
                and request.FILES.get("file")
                and request.FILES["file"].size > settings.PANDECT_MAX_FILE_BYTES
                else 400
            )
            return _error(
                request,
                "validation_error",
                "Review the highlighted fields.",
                status,
                form.errors.get_json_data(),
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
        "max_text_bytes": settings.PANDECT_MAX_TEXT_BYTES,
        "max_batch_files": settings.PANDECT_MAX_BATCH_FILES,
        "max_file_bytes": settings.PANDECT_MAX_FILE_BYTES,
    }
    return render(request, "core/evidence/form.html", context, status=status)


@product_login_required
@require_http_methods(["GET", "POST"])
def edit(request, case_id, kind, evidence_id):
    is_post = request.method == "POST"
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=is_post)
        item = get_evidence(case, kind, evidence_id, lock=is_post)
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
                messages.success(
                    request, "Evidence details saved. The original is unchanged."
                )
                return redirect(_info_url(request, case, kind, item))
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
    return render(request, "core/evidence/form.html", context, status=status)


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


def _file_download(request, case, kind, item, *, attachment=None):
    source = attachment if attachment is not None else item
    try:
        original = source.file.open("rb")
    except OSError, ValueError:
        return render(
            request,
            "core/evidence/unavailable.html",
            case_context(case, request.user) | {"item": item, "kind": kind},
            status=503,
        )
    try:
        filename = get_valid_filename(source.original_filename)
    except SuspiciousFileOperation:
        prefix = "audio" if attachment is not None else "evidence"
        filename = f"{prefix}-{source.pk}"
    return FileResponse(
        original,
        as_attachment=True,
        filename=filename,
        content_type="application/octet-stream",
    )


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
        response = _file_download(request, case, kind, item)
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
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=True)
        item = get_evidence(case, kind, evidence_id, lock=True)
        if item.is_archived != archived:
            item.is_archived = archived
            item.archive_changed_at = timezone.now()
            item.archive_changed_by = request.user
            item.save(
                update_fields=[
                    "is_archived",
                    "archive_changed_at",
                    "archive_changed_by",
                ]
            )
            if kind == "vehicle-location":
                from geo.trip_processing import invalidate_vehicle_trips

                invalidate_vehicle_trips(request.user, case, [item.vehicle_id])
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
        plan = plan_deletion(item, kind)
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

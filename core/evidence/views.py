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
from django.core.paginator import Paginator
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import CharField, Q, Value
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
from core.analysis.automatic import evidence_runs, start_added_evidence_analysis
from core.analysis.errors import AnalysisError
from core.analysis.search import derived_content_condition, derived_text_matches
from core.evidence.deletion import (
    CHANGED,
    deletion_manifest_response,
    deletion_review_context,
    get_deletion_target,
    plan_deletion,
)
from core.evidence.form_registry import TYPE_FIELDS, evidence_form
from core.evidence.forms import COMMON_FIELDS, SOURCE_FIELDS, EvidenceDeleteForm
from core.evidence.navigation import list_params, query_suffix
from core.evidence.queries import duplicate_matches, get_evidence, scoped_evidence
from core.evidence.search import retained_content_condition, retained_text_matches
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

SAVE_UNCONFIRMED = (
    "The save could not be confirmed. Keep your original and retry this submission."
)


def _private(response):
    response["Cache-Control"] = "private, no-store"
    return response


def _error(request, code, message, status=400, errors=None):
    if wants_json(request):
        return _private(
            JsonResponse(
                {"code": code, "message": message, "errors": errors or {}},
                status=status,
            )
        )
    return _private(
        render(
            request,
            "core/evidence/error.html",
            {"error_message": message},
            status=status,
        )
    )


def _detail_url(case, kind, item):
    return reverse(
        "core:evidence:detail",
        kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
    )


def _list_context(request, case):
    query = request.GET.get("q", "")[:200].strip()
    selected_kind = request.GET.get("type", "")
    if selected_kind not in KINDS:
        selected_kind = ""
    state = request.GET.get("state", "active")
    if state not in {"active", "archived", "all"}:
        state = "active"
    queries = []
    for kind in KINDS:
        if selected_kind and kind != selected_kind:
            continue
        queryset = scoped_evidence(case, kind)
        if state != "all":
            queryset = queryset.filter(is_archived=state == "archived")
        if query:
            condition = (
                Q(title__icontains=query)
                | Q(description__icontains=query)
                | Q(source__icontains=query)
                | Q(source_description__icontains=query)
                | Q(source_url__icontains=query)
            )
            if kind in FILE_KINDS:
                condition |= Q(original_filename__icontains=query)
            if kind in LOCATION_KINDS:
                condition |= Q(address__icontains=query)
            if kind in COMMUNICATION_KINDS:
                for participant in ("sender", "recipient"):
                    for field in ("first_name", "last_name", "email", "phone_number"):
                        condition |= Q(**{f"{participant}__{field}__icontains": query})
            queryset, content_condition = retained_content_condition(
                queryset, case=case, kind=kind, query=query
            )
            condition |= content_condition
            queryset, extracted_condition = derived_content_condition(
                queryset, case=case, kind=kind, query=query
            )
            condition |= extracted_condition
            if kind == "person-location":
                condition |= (
                    Q(person__first_name__icontains=query)
                    | Q(person__last_name__icontains=query)
                    | Q(person__email__icontains=query)
                    | Q(person__phone_number__icontains=query)
                )
            if kind == "vehicle-location":
                condition |= (
                    Q(vehicle__make__icontains=query)
                    | Q(vehicle__model__icontains=query)
                    | Q(vehicle__license_plate__icontains=query)
                    | Q(vehicle__vin__icontains=query)
                    | Q(raw_row__Asset__icontains=query)
                    | Q(**{"raw_row__Serial Number__icontains": query})
                )
            queryset = queryset.filter(condition)
        queries.append(
            queryset.order_by()
            .annotate(kind=Value(kind, output_field=CharField()))
            .values("id", "title", "created_at", "is_archived", "kind", "sha256")
        )
    combined = (
        queries[0].union(*queries[1:], all=True).order_by("-created_at", "kind", "id")
    )
    page = Paginator(combined, 25).get_page(request.GET.get("page"))
    params = list_params(request)
    suffix = query_suffix(request)
    rows = []
    for row in page.object_list:
        row["label"] = KIND_LABELS[row["kind"]]
        row["url"] = (
            reverse(
                "core:evidence:detail",
                kwargs={
                    "case_id": case.pk,
                    "kind": row["kind"],
                    "evidence_id": row["id"],
                },
            )
            + suffix
        )
        rows.append(row)
    matches = derived_text_matches(case=case, rows=rows, query=query)
    matches.update(retained_text_matches(case=case, rows=rows, query=query))
    for row in rows:
        row["content_match"] = matches.get((row["kind"], row["id"]))
        if row["content_match"] and row["content_match"].get("url"):
            row["url"] = row["content_match"]["url"]
        if row["content_match"] and row["content_match"].get("anchor"):
            row["url"] += "#" + row["content_match"]["anchor"]
    pagination = {}
    for name, number in (
        ("previous", page.previous_page_number() if page.has_previous() else None),
        ("next", page.next_page_number() if page.has_next() else None),
    ):
        if number is not None:
            page_params = params.copy()
            page_params["page"] = number
            pagination[name] = "?" + page_params.urlencode()
    return {
        "rows": rows,
        "page_obj": page,
        "pagination": pagination,
        "query": query,
        "selected_kind": selected_kind,
        "state": state,
        "kind_choices": KIND_LABELS.items(),
        "list_suffix": suffix,
        "list_url": reverse("core:evidence:list", kwargs={"case_id": case.pk}) + suffix,
    }


@product_login_required
@require_safe
def evidence_list(request, case_id):
    case = get_case(request.user, case_id)
    context = case_context(case, request.user) | _list_context(request, case)
    context["nav_section"] = "evidence"
    return _private(render(request, "core/evidence/workspace.html", context))


@product_login_required
@require_safe
def detail(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    item = get_evidence(case, kind, evidence_id)
    context = case_context(case, request.user) | _list_context(request, case)
    section = "details" if request.GET.get("section") == "details" else "original"
    technical = [
        (item._meta.get_field(name).verbose_name.capitalize(), getattr(item, name))
        for name in TYPE_FIELDS[kind]
        if getattr(item, name) not in (None, "")
    ]
    context.update(
        {
            "item": item,
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
            "reader_template": READER_TEMPLATES[kind],
            "technical_details": technical,
            "section": section,
            "nav_section": "evidence",
            "is_location": kind in LOCATION_KINDS,
            "is_communication": kind in COMMUNICATION_KINDS,
            "is_call": kind in CALL_KINDS,
            "can_delete_evidence": request.user.is_superuser,
        }
    )
    if section == "original":
        runs = evidence_runs(case, kind, item.pk)
        context["analysis_pending"] = runs.filter(
            status__in=("queued", "running")
        ).exists()
        context["has_analysis"] = context["analysis_pending"] or runs.exists()
        if kind in COMMUNICATION_KINDS:
            from phone.presentation import reader_context

            context.update(reader_context(case, kind, item))
    if kind == "document":
        context["vehicle_import"] = VehicleLocationImport.objects.filter(
            case=case, document=item
        ).first()
    return _private(render(request, "core/evidence/workspace.html", context))


def _created_response(request, case, kind, item, *, created):
    url = _detail_url(case, kind, item)
    if wants_json(request):
        return _private(
            JsonResponse(
                {"type": kind, "id": str(item.pk), "detail_url": url},
                status=201 if created else 200,
            )
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
        "confirmation": signing.dumps(identity, salt="core.evidence.duplicate"),
    }


@product_login_required
@require_safe
def bulk_create(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    form = evidence_form("document")(
        kind="document", instance=KINDS["document"](case=case, uploaded_by=request.user)
    )
    context = case_context(case, request.user) | {
        "shared_fields": [form[name] for name in SOURCE_FIELDS],
        "max_batch_files": settings.PANDECT_MAX_BATCH_FILES,
        "max_file_bytes": settings.PANDECT_MAX_FILE_BYTES,
    }
    return _private(render(request, "core/evidence/bulk_form.html", context))


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
    kind = (
        request.POST.get("kind", "document")
        if request.method == "POST"
        else request.GET.get("kind", "document")
    )
    if kind not in KINDS:
        return _error(request, "invalid_type", "Choose a supported evidence type.")
    form_class = evidence_form(kind)
    form = form_class(
        kind=kind, instance=KINDS[kind](case=case, uploaded_by=request.user)
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
                            return _private(
                                JsonResponse(
                                    {"code": "duplicate_evidence", **duplicates},
                                    status=409,
                                )
                            )
                        status = 409
                    else:
                        item = form.save()
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
                        if kind not in CALL_KINDS or attachment:
                            start_added_evidence_analysis(
                                request.user,
                                case.pk,
                                kind,
                                item.pk,
                                attachment_id=attachment.pk if attachment else None,
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
                and request.FILES["file"].size
                > getattr(settings, "PANDECT_MAX_FILE_BYTES", 100 * 1024 * 1024)
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
        "has_people": kind not in COMMUNICATION_KINDS
        or form.fields["sender"].queryset.exists(),
        "nav_section": "evidence",
        "max_text_bytes": settings.PANDECT_MAX_TEXT_BYTES,
        "max_batch_files": getattr(settings, "PANDECT_MAX_BATCH_FILES", 20),
        "max_file_bytes": getattr(
            settings, "PANDECT_MAX_FILE_BYTES", 100 * 1024 * 1024
        ),
    }
    return _private(render(request, "core/evidence/form.html", context, status=status))


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
                return redirect(_detail_url(case, kind, item) + query_suffix(request))
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
            "conflict": conflict,
            "latest_details": latest_details if conflict else [],
        }
    return _private(render(request, "core/evidence/form.html", context, status=status))


@product_login_required
@require_safe
def preview(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    if kind != "image":
        raise Http404
    item = get_evidence(case, kind, evidence_id)
    try:
        content = image_preview(item)
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
    return _private(response)


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
    return _private(response)


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
    messages.success(
        request,
        "Evidence archived. Its original is retained."
        if archived
        else "Evidence restored to the active collection.",
    )
    params = list_params(request)
    if request.GET.get("section") == "details":
        params["section"] = "details"
    suffix = "?" + params.urlencode() if params else ""
    return redirect(_detail_url(case, kind, item) + suffix)


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
                plan.collector.delete()
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
    params = list_params(request)
    params["section"] = "details"
    review_context = deletion_review_context(request, plan)
    if review_context["inventory_changed"]:
        status = 409
    return _private(
        render(
            request,
            "core/evidence/delete.html",
            {
                **case_context(case, request.user),
                "item": item,
                "form": form,
                "deletion_blocker": plan.blocker,
                "analysis_count": plan.analysis_count,
                **review_context,
                "cancel_url": _detail_url(case, kind, item) + "?" + params.urlencode(),
                "nav_section": "evidence",
            },
            status=status,
        )
    )

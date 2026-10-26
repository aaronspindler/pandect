"""Case-authorized investigator workflows for retained original evidence."""

from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, SuspiciousFileOperation
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
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
from config.access import product_login_required

from .forms import (
    COMMON_FIELDS,
    FILE_KINDS,
    KIND_LABELS,
    KINDS,
    LOCATION_KINDS,
    TYPE_FIELDS,
    evidence_form,
)


def _wants_json(request):
    return "application/json" in request.headers.get("Accept", "")


def _private(response):
    response["Cache-Control"] = "private, no-store"
    return response


def _error(request, code, message, status=400, errors=None):
    if _wants_json(request):
        return _private(
            JsonResponse(
                {"code": code, "message": message, "errors": errors or {}},
                status=status,
            )
        )
    return _private(
        render(
            request, "evidence/error.html", {"error_message": message}, status=status
        )
    )


def _model(kind):
    try:
        return KINDS[kind]
    except KeyError:
        raise Http404("Evidence not found.") from None


def _record(case, kind, evidence_id, *, lock=False):
    queryset = _model(kind).objects.filter(case=case)
    if kind == "person-location":
        queryset = queryset.filter(person__case=case).select_related("person")
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    try:
        return queryset.select_related("uploaded_by", "archive_changed_by").get(
            pk=evidence_id
        )
    except _model(kind).DoesNotExist:
        raise Http404("Evidence not found.") from None


def _detail_url(case, kind, item):
    return reverse(
        "evidence:detail",
        kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
    )


def _query_suffix(request):
    params = request.GET.copy()
    for key in list(params):
        if key not in {"q", "type", "state", "page"}:
            del params[key]
    if "q" in params:
        params["q"] = params["q"][:200]
    value = params.urlencode()
    return f"?{value}" if value else ""


def _list_context(request, case):
    query = request.GET.get("q", "")[:200].strip()
    selected_kind = request.GET.get("type", "")
    if selected_kind not in KINDS:
        selected_kind = ""
    state = request.GET.get("state", "active")
    if state not in {"active", "archived", "all"}:
        state = "active"
    queries = []
    for kind, model in KINDS.items():
        if selected_kind and kind != selected_kind:
            continue
        queryset = model.objects.filter(case=case)
        if kind == "person-location":
            queryset = queryset.filter(person__case=case)
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
            if kind == "person-location":
                condition |= (
                    Q(person__first_name__icontains=query)
                    | Q(person__last_name__icontains=query)
                    | Q(person__email__icontains=query)
                    | Q(person__phone_number__icontains=query)
                )
            queryset = queryset.filter(condition)
        queries.append(
            queryset.order_by()
            .annotate(kind=Value(kind, output_field=CharField()))
            .values("id", "title", "created_at", "is_archived", "kind")
        )
    combined = (
        queries[0].union(*queries[1:], all=True).order_by("-created_at", "kind", "id")
    )
    page = Paginator(combined, 25).get_page(request.GET.get("page"))
    params = request.GET.copy()
    for key in list(params):
        if key not in {"q", "type", "state", "page"}:
            del params[key]
    suffix = _query_suffix(request)
    rows = []
    for row in page.object_list:
        row["label"] = KIND_LABELS[row["kind"]]
        row["url"] = (
            reverse(
                "evidence:detail",
                kwargs={
                    "case_id": case.pk,
                    "kind": row["kind"],
                    "evidence_id": row["id"],
                },
            )
            + suffix
        )
        rows.append(row)
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
        "list_url": reverse("evidence:list", kwargs={"case_id": case.pk}) + suffix,
    }


@product_login_required
@require_safe
def evidence_list(request, case_id):
    case = get_case(request.user, case_id)
    context = case_context(case, request.user) | _list_context(request, case)
    context["nav_section"] = "evidence"
    return _private(render(request, "evidence/workspace.html", context))


@product_login_required
@require_safe
def detail(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    item = _record(case, kind, evidence_id)
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
            "technical_details": technical,
            "section": section,
            "nav_section": "evidence",
            "is_location": kind in LOCATION_KINDS,
        }
    )
    return _private(render(request, "evidence/workspace.html", context))


def _created_response(request, case, kind, item, *, created):
    url = _detail_url(case, kind, item)
    if _wants_json(request):
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


@product_login_required
@require_safe
def bulk_create(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    form = evidence_form("document")(
        kind="document", instance=KINDS["document"](case=case, uploaded_by=request.user)
    )
    context = case_context(case, request.user) | {
        "shared_fields": [
            form[name]
            for name in ("source", "source_url", "source_description", "collected_at")
        ],
        "max_batch_files": settings.PANDECT_MAX_BATCH_FILES,
        "max_file_bytes": settings.PANDECT_MAX_FILE_BYTES,
    }
    return _private(render(request, "evidence/bulk_form.html", context))


@product_login_required
@require_http_methods(["GET", "POST"])
def create(request, case_id):
    try:
        case = get_case(request.user, case_id, edit=True)
    except PermissionDenied:
        if _wants_json(request):
            return _error(
                request,
                "permission_denied",
                "You no longer have permission to add evidence.",
                403,
            )
        raise
    except Http404:
        if _wants_json(request):
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
        try:
            with transaction.atomic():
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
                    item = form.save()
                    return _created_response(request, case, kind, item, created=True)
                status = 400
        except PermissionDenied:
            if not _wants_json(request):
                raise
            return _error(
                request,
                "permission_denied",
                "You no longer have permission to add evidence.",
                403,
            )
        except Http404:
            if not _wants_json(request):
                raise
            return _error(request, "not_found", "This case is unavailable.", 404)
        except OSError:
            message = "The original could not be saved. Keep your file and try again."
            if _wants_json(request):
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
        if _wants_json(request):
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
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "kind_choices": KIND_LABELS.items(),
        "is_file": kind in FILE_KINDS,
        "nav_section": "evidence",
        "max_text_bytes": settings.PANDECT_MAX_TEXT_BYTES,
        "max_batch_files": getattr(settings, "PANDECT_MAX_BATCH_FILES", 20),
        "max_file_bytes": getattr(
            settings, "PANDECT_MAX_FILE_BYTES", 100 * 1024 * 1024
        ),
    }
    return _private(render(request, "evidence/form.html", context, status=status))


@product_login_required
@require_http_methods(["GET", "POST"])
def edit(request, case_id, kind, evidence_id):
    with transaction.atomic():
        case = get_case(request.user, case_id, edit=True, lock=True)
        item = _record(case, kind, evidence_id, lock=True)
        saved_timestamp = item.edited_at
        latest_details = [
            (item._meta.get_field(name).verbose_name.capitalize(), getattr(item, name))
            for name in COMMON_FIELDS + TYPE_FIELDS[kind]
        ]
        conflict = False
        form = evidence_form(kind, editing=True)(
            request.POST if request.method == "POST" else None, kind=kind, instance=item
        )
        status = 200
        if request.method == "POST" and form.is_valid():
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
                return redirect(_detail_url(case, kind, item) + _query_suffix(request))
        elif request.method == "POST":
            status = 400
        context = case_context(case, request.user) | {
            "form": form,
            "item": item,
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
            "editing": True,
            "nav_section": "evidence",
            "list_suffix": _query_suffix(request),
            "conflict": conflict,
            "latest_details": latest_details if conflict else [],
        }
        return _private(render(request, "evidence/form.html", context, status=status))


@product_login_required
@require_safe
def download(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    item = _record(case, kind, evidence_id)
    if kind == "text":
        response = HttpResponse(
            item.body.encode("utf-8"), content_type="text/plain; charset=utf-8"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="evidence-{item.pk}.txt"'
        )
    elif kind in LOCATION_KINDS:
        response = HttpResponse(item.original_bytes(), content_type="application/json")
        response["Content-Disposition"] = (
            f'attachment; filename="evidence-{item.pk}.json"'
        )
    else:
        try:
            original = item.file.open("rb")
        except OSError, ValueError:
            return _private(
                render(
                    request,
                    "evidence/unavailable.html",
                    case_context(case, request.user) | {"item": item, "kind": kind},
                    status=503,
                )
            )
        try:
            filename = get_valid_filename(item.original_filename)
        except SuspiciousFileOperation:
            filename = f"evidence-{item.pk}"
        response = FileResponse(
            original,
            as_attachment=True,
            filename=filename,
            content_type="application/octet-stream",
        )
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
        item = _record(case, kind, evidence_id, lock=True)
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
    return redirect(_detail_url(case, kind, item) + _query_suffix(request))

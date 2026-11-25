"""Retain case CSVs, then review and publish them through background jobs."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from geo.import_forms import VehicleImportReviewForm, VehicleImportUploadForm
from geo.models import VehicleLocationImport
from geo.vehicle_import_jobs import (
    ERROR_MESSAGES,
    ImportSubmissionConflict,
    enqueue_completion,
    enqueue_preparation,
    retain_import,
    retry_import,
)
from geo.vehicle_imports import MAX_BYTES, MAX_GROUPS, MAX_ROWS
from ui.live import render_live


@product_login_required
@require_http_methods(["GET", "POST"])
def vehicle_import_upload(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    vehicle_hint = None
    if value := request.GET.get("vehicle"):
        try:
            vehicle_hint = case.vehicles.filter(pk=value).first()
        except ValidationError:
            pass
    form = VehicleImportUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        case=case,
        initial={"vehicle": vehicle_hint},
    )
    status = 200
    if request.method == "POST":
        status = 400
        if getattr(request, "evidence_upload_too_large", False):
            form.add_error(None, "Choose a CSV within the displayed import size limit.")
        elif form.is_valid():
            try:
                batch, created = retain_import(
                    request.user,
                    case_id,
                    submission_id=form.cleaned_data["submission_id"],
                    original=form.cleaned_data["file"],
                    timezone=form.cleaned_data["timezone"],
                    vehicle=form.cleaned_data["vehicle"],
                    source=form.cleaned_data["source"],
                )
            except ImportSubmissionConflict as error:
                form.add_error(None, error)
                status = 409
            except OSError, ValidationError:
                form.add_error(
                    None,
                    "The CSV could not be retained. Select it again and retry; "
                    "no observations were imported.",
                )
                status = 503
            else:
                if created:
                    messages.success(
                        request,
                        "CSV retained. Its review is being prepared in the background.",
                    )
                return redirect(
                    "geo:vehicle_import_review", case_id=case.pk, import_id=batch.pk
                )
    return render(
        request,
        "geo/import_upload.html",
        {
            **case_context(case, request.user),
            "form": form,
            "launch_vehicle": vehicle_hint,
            "nav_section": "evidence",
            "max_csv_bytes": min(MAX_BYTES, settings.PANDECT_MAX_FILE_BYTES),
            "max_csv_rows": f"{MAX_ROWS:,}",
            "max_csv_groups": MAX_GROUPS,
        },
        status=status,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def vehicle_import_review(request, case_id, import_id):
    case = get_case(request.user, case_id, edit=request.method == "POST")
    batch = get_object_or_404(
        VehicleLocationImport.objects.filter(
            case=case, document__case=case
        ).select_related("document", "uploaded_by", "selected_vehicle"),
        pk=import_id,
    )
    context = case_context(case, request.user)
    form = None
    status = 200
    error_message = ""
    review_url = "geo:vehicle_import_review"
    draft_key = f"vehicle_import_review_draft:{batch.pk}"
    initial = {"confirm_duplicates": batch.duplicates_confirmed}
    if request.method == "GET":
        initial.update(request.session.pop(draft_key, {}))
    if batch.completed_at:
        if request.method == "POST":
            messages.success(
                request,
                "This import was already completed. No observations were added again.",
            )
            return redirect(review_url, case_id=case.pk, import_id=batch.pk)
    if (
        context["can_edit"]
        and batch.status == "review"
        and not batch.completed_at
        and not batch.document.is_archived
    ):
        form = VehicleImportReviewForm(
            request.POST if request.method == "POST" else None,
            case=case,
            groups=batch.groups,
            duplicates=batch.duplicate_count > 0,
            default_vehicle=batch.selected_vehicle_id,
            vehicle_mapping=batch.vehicle_mapping,
            initial=initial,
        )
    if request.method == "POST" and not batch.completed_at:
        try:
            if batch.document.is_archived:
                raise ValidationError(ERROR_MESSAGES["source_archived"])
            if request.POST.get("action") == "prepare":
                enqueue_preparation(request.user, case.pk, batch.pk)
                messages.success(
                    request, "The review is being prepared in the background."
                )
                return redirect(review_url, case_id=case.pk, import_id=batch.pk)
            if request.POST.get("action") == "retry":
                retry_import(
                    request.user,
                    case.pk,
                    batch.pk,
                    worker_stopped=request.POST.get("worker_stopped") == "on",
                )
                messages.success(
                    request, "The import has been queued for another attempt."
                )
                return redirect(review_url, case_id=case.pk, import_id=batch.pk)
            if batch.phase == "publish" and batch.status in {"queued", "running"}:
                messages.success(
                    request,
                    "This import is already queued. No observations were added again.",
                )
                return redirect(review_url, case_id=case.pk, import_id=batch.pk)
            if (
                request.POST.get("action") == "add_vehicle"
                and form
                and not batch.invalid_count
            ):
                # Temporarily carry unfinished choices through the vehicle form.
                # Confirmation still requires reviewing and submitting the import.
                draft = {}
                for index in range(len(batch.groups)):
                    name = f"vehicle_{index}"
                    value = form[name].value()
                    draft[name] = value if form.fields[name].valid_value(value) else ""
                if "confirm_duplicates" in form.fields:
                    draft["confirm_duplicates"] = form["confirm_duplicates"].value()
                request.session[draft_key] = draft
                return redirect(
                    "vehicle:create_for_import", case_id=case.pk, import_id=batch.pk
                )
            if form and form.is_valid() and not batch.invalid_count:
                mapping = {
                    str(index): form.cleaned_data[f"vehicle_{index}"]
                    for index in range(len(batch.groups))
                }
                enqueue_completion(
                    request.user,
                    case.pk,
                    batch.pk,
                    mapping,
                    confirm_duplicates=form.cleaned_data.get(
                        "confirm_duplicates", False
                    ),
                )
                messages.success(
                    request,
                    "Vehicle locations queued for import. Follow progress below.",
                )
                return redirect(review_url, case_id=case.pk, import_id=batch.pk)
            status = 400
            if not form:
                error_message = (
                    "This import is not ready to confirm. Refresh its status."
                )
            elif batch.invalid_count:
                form.add_error(
                    None,
                    "Correct the invalid rows and upload a new CSV. "
                    "This source remains retained.",
                )
        except ValidationError as error:
            status = 400
            error_message = " ".join(error.messages)
            batch.refresh_from_db()
    rows_page = None
    observations_page = None
    invalid = (
        batch.status == "review" and not batch.completed_at and batch.invalid_count
    )
    correction_only = bool(invalid and request.GET.get("rows") != "all")
    if batch.completed_at:
        observations = (
            batch.observations.filter(case=case, vehicle__case_id=F("case_id"))
            .select_related("vehicle")
            .order_by("source_row")
        )
        observations_page = Paginator(observations, 50).get_page(
            request.GET.get("page")
        )
    elif batch.status == "review" and not batch.document.is_archived:
        rows = batch.rows.order_by("number")
        if correction_only:
            rows = rows.exclude(errors=[])
        rows_page = Paginator(rows, 50).get_page(request.GET.get("page"))
    skipped_count = batch.duplicate_count if not batch.duplicates_confirmed else 0
    return render_live(
        request,
        "geo/import_review.html",
        {
            **context,
            "batch": batch,
            "form": form,
            "rows": rows_page,
            "invalid": bool(invalid),
            "correction_only": correction_only,
            "observations": observations_page,
            "imported_count": batch.row_count - skipped_count,
            "skipped_count": skipped_count,
            "nonduplicate_count": batch.row_count - batch.duplicate_count,
            "page_obj": observations_page or rows_page,
            "error_message": error_message,
            "processing_error": ERROR_MESSAGES.get(
                batch.error_code,
                "The import could not finish. Review the source and try again.",
            ),
            "progress": min(100, batch.processed_rows * 100 // batch.row_count)
            if batch.row_count and batch.phase != "publish"
            else None,
            "can_retry": context["can_edit"]
            and not batch.document.is_archived
            and batch.attempts < 3,
            "nav_section": "evidence",
        },
        status=status,
    )

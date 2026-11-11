"""Retain case CSVs, then review and publish them through background jobs."""

import hashlib

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.storage import retain_uploaded_originals
from document.models import DocumentEvidence
from geo.import_forms import VehicleImportReviewForm, VehicleImportUploadForm
from geo.models import VehicleLocationImport
from geo.vehicle_import_jobs import (
    ERROR_MESSAGES,
    enqueue_completion,
    enqueue_preparation,
    retry_import,
)
from geo.vehicle_imports import MAX_BYTES, MAX_GROUPS, MAX_ROWS, PARSER_VERSION


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
def vehicle_import_upload(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    form = VehicleImportUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        case=case,
    )
    status = 200
    if request.method == "POST":
        status = 400
        if getattr(request, "evidence_upload_too_large", False):
            form.add_error(None, "Choose a CSV within the displayed import size limit.")
        elif form.is_valid():
            original = form.cleaned_data["file"]
            vehicle = form.cleaned_data["vehicle"]
            try:
                with retain_uploaded_originals() as originals:
                    case = get_case(request.user, case_id, edit=True, lock=True)
                    batch = (
                        VehicleLocationImport.objects.filter(
                            pk=form.cleaned_data["submission_id"]
                        )
                        .select_related("document")
                        .first()
                    )
                    if batch:
                        digest = hashlib.sha256()
                        for chunk in original.chunks():
                            digest.update(chunk)
                        if (
                            batch.case_id != case.pk
                            or batch.uploaded_by_id != request.user.pk
                            or batch.document.case_id != case.pk
                            or batch.document.sha256 != digest.hexdigest()
                            or batch.timezone != form.cleaned_data["timezone"]
                            or batch.selected_vehicle_id != getattr(vehicle, "pk", None)
                        ):
                            form.add_error(
                                None,
                                "This submission cannot be reused. Start a new import.",
                            )
                            status = 409
                        else:
                            return redirect(
                                "geo:vehicle_import_review",
                                case_id=case.pk,
                                import_id=batch.pk,
                            )
                    else:
                        document = DocumentEvidence(
                            case=case,
                            uploaded_by=request.user,
                            title=f"Asset Location CSV: {original.name}"[:255],
                            source=form.cleaned_data["source"],
                            file=original,
                        )
                        document.full_clean()
                        document.save()
                        originals.append(document.file)
                        batch = VehicleLocationImport(
                            id=form.cleaned_data["submission_id"],
                            case=case,
                            uploaded_by=request.user,
                            document=document,
                            timezone=form.cleaned_data["timezone"],
                            parser_version=PARSER_VERSION,
                            selected_vehicle=vehicle,
                        )
                        batch.save()
                        enqueue_preparation(request.user, case.pk, batch.pk)
                if originals:
                    messages.success(
                        request,
                        "CSV retained. Its review is being prepared in the background.",
                    )
                    return redirect(
                        "geo:vehicle_import_review",
                        case_id=case.pk,
                        import_id=batch.pk,
                    )
            except OSError, ValidationError:
                form.add_error(
                    None,
                    "The CSV could not be retained. Select it again and retry; "
                    "no observations were imported.",
                )
                status = 503
    return render(
        request,
        "geo/import_upload.html",
        {
            **case_context(case, request.user),
            "form": form,
            "nav_section": "evidence",
            "max_csv_bytes": min(MAX_BYTES, settings.PANDECT_MAX_FILE_BYTES),
            "max_csv_rows": f"{MAX_ROWS:,}",
            "max_csv_groups": MAX_GROUPS,
        },
        status=status,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
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
    if batch.completed_at:
        if request.method == "POST":
            messages.success(
                request,
                "This import was already completed. No observations were added again.",
            )
            return redirect(review_url, case_id=case.pk, import_id=batch.pk)
    if context["can_edit"] and batch.status == "review" and not batch.completed_at:
        form = VehicleImportReviewForm(
            request.POST if request.method == "POST" else None,
            case=case,
            groups=batch.groups,
            duplicates=batch.duplicate_count > 0,
            default_vehicle=batch.selected_vehicle_id,
            vehicle_mapping=batch.vehicle_mapping,
            initial={"confirm_duplicates": batch.duplicates_confirmed},
        )
    if request.method == "POST" and not batch.completed_at:
        try:
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
                    "Vehicle locations queued for import. Refresh to follow progress.",
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
    if batch.completed_at:
        observations = (
            batch.observations.filter(case=case, vehicle__case_id=F("case_id"))
            .select_related("vehicle")
            .order_by("source_row")
        )
        observations_page = Paginator(observations, 50).get_page(
            request.GET.get("page")
        )
    elif batch.status == "review":
        rows_page = Paginator(batch.rows.order_by("number"), 50).get_page(
            request.GET.get("page")
        )
    skipped_count = batch.duplicate_count if not batch.duplicates_confirmed else 0
    return render(
        request,
        "geo/import_review.html",
        {
            **context,
            "batch": batch,
            "form": form,
            "rows": rows_page,
            "invalid": batch.invalid_count > 0,
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
            "can_retry": context["can_edit"] and batch.attempts < 3,
            "nav_section": "evidence",
        },
        status=status,
    )

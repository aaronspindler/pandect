"""Two-step, case-authorized CSV ingestion with atomic, repeatable completion."""

import hashlib

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required

from .import_forms import VehicleImportReviewForm, VehicleImportUploadForm
from .models import DocumentEvidence, VehicleLocationEvidence, VehicleLocationImport
from .signals import delete_original
from .vehicle_imports import MAX_BYTES, PARSER_VERSION, read_import, row_fingerprint


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
def vehicle_import_upload(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    form = VehicleImportUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    document = None
    status = 200
    if request.method == "POST":
        status = 400
        if getattr(request, "evidence_upload_too_large", False):
            form.add_error(None, "Choose a CSV within the displayed import size limit.")
        elif form.is_valid():
            original = form.cleaned_data["file"]
            digest = hashlib.sha256(original.read()).hexdigest()
            original.seek(0)
            try:
                with transaction.atomic():
                    case = get_case(request.user, case_id, edit=True, lock=True)
                    batch = (
                        VehicleLocationImport.objects.filter(
                            pk=form.cleaned_data["submission_id"]
                        )
                        .select_related("document")
                        .first()
                    )
                    if batch:
                        if (
                            batch.case_id != case.pk
                            or batch.uploaded_by_id != request.user.pk
                            or batch.document.sha256 != digest
                            or batch.timezone != form.cleaned_data["timezone"]
                        ):
                            form.add_error(
                                None,
                                "This submission cannot be reused. Start a new import.",
                            )
                            status = 409
                        else:
                            return redirect(
                                "evidence:vehicle_import_review",
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
                        batch = VehicleLocationImport(
                            id=form.cleaned_data["submission_id"],
                            case=case,
                            uploaded_by=request.user,
                            document=document,
                            timezone=form.cleaned_data["timezone"],
                            parser_version=PARSER_VERSION,
                        )
                        batch.save()
                        return redirect(
                            "evidence:vehicle_import_review",
                            case_id=case.pk,
                            import_id=batch.pk,
                        )
            except Exception as error:
                # File storage is outside the database transaction. Remove only a
                # new file whose database record was rolled back.
                if document and document.file._committed:
                    delete_original(document.file.storage, document.file.name)
                if not isinstance(error, (OSError, ValidationError)):
                    raise
                form.add_error(
                    None,
                    "The CSV could not be retained. Select it again and retry; "
                    "no observations were imported.",
                )
                status = 503
    return render(
        request,
        "evidence/import_upload.html",
        {
            **case_context(case, request.user),
            "form": form,
            "nav_section": "evidence",
            "max_csv_bytes": min(MAX_BYTES, settings.PANDECT_MAX_FILE_BYTES),
        },
        status=status,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
@never_cache
def vehicle_import_review(request, case_id, import_id):
    status = 200
    rows = []
    form = None
    error_message = ""
    with transaction.atomic():
        case = get_case(
            request.user,
            case_id,
            edit=request.method == "POST",
            lock=request.method == "POST",
        )
        batches = VehicleLocationImport.objects.filter(
            case=case, document__case=case
        ).select_related("document", "uploaded_by")
        if request.method == "POST":
            batches = batches.select_for_update(of=("self",))
        batch = get_object_or_404(batches, pk=import_id)
        if batch.completed_at is not None:
            if request.method == "POST":
                messages.success(
                    request,
                    "This import was already completed. "
                    "No observations were added again.",
                )
                return redirect(
                    "evidence:vehicle_import_review",
                    case_id=case.pk,
                    import_id=batch.pk,
                )
        else:
            try:
                rows = read_import(batch)
            except OSError, ValidationError:
                error_message = (
                    "The retained CSV could not be read or verified. "
                    "No observations were imported. Restore the original file "
                    "or upload a corrected CSV."
                )
                status = 503
            if rows:
                groups = list(dict.fromkeys(row.group for row in rows))
                existing = {
                    row_fingerprint(raw)
                    for raw in VehicleLocationEvidence.objects.filter(
                        case=case,
                        vehicle__case=case,
                        import_batch__case=case,
                        import_batch__timezone=batch.timezone,
                        raw_row__in=[row.raw for row in rows],
                    )
                    .values_list("raw_row", flat=True)
                    .distinct()
                }
                for row in rows:
                    row.duplicate |= row.fingerprint in existing
                form = VehicleImportReviewForm(
                    request.POST if request.method == "POST" else None,
                    case=case,
                    groups=groups,
                    duplicates=any(row.duplicate for row in rows),
                )
                if request.method == "POST":
                    status = 400
                    valid = form.is_valid()
                    if any(row.errors for row in rows):
                        form.add_error(
                            None,
                            "Correct the invalid rows and upload a new CSV. "
                            "This source remains retained.",
                        )
                    elif valid:
                        mapping = {
                            group: form.cleaned_data[f"vehicle_{index}"]
                            for index, group in enumerate(groups)
                        }
                        try:
                            # The savepoint rolls back every observation on any
                            # validation/database failure; the source stays retained.
                            with transaction.atomic():
                                for row in rows:
                                    observed_label = row.values["observed_at"].strftime(
                                        "%d %b %Y, %H:%M:%S UTC"
                                    )
                                    asset_label = (row.asset or row.serial)[:180]
                                    item = VehicleLocationEvidence(
                                        case=case,
                                        uploaded_by=request.user,
                                        title=f"{asset_label} · {observed_label}",
                                        source=batch.document.source,
                                        vehicle=mapping[row.group],
                                        import_batch=batch,
                                        source_row=row.number,
                                        raw_row=row.raw,
                                        **row.values,
                                    )
                                    item.full_clean()
                                    item.save()
                                batch.vehicle_mapping = {
                                    str(index): str(mapping[group].pk)
                                    for index, group in enumerate(groups)
                                }
                                batch.completed_at = timezone.now()
                                batch.save(
                                    update_fields=("vehicle_mapping", "completed_at")
                                )
                        except ValidationError, IntegrityError:
                            batch.refresh_from_db()
                            form.add_error(
                                None,
                                "The observations could not be validated. "
                                "No observations were imported. "
                                "Review the source and vehicle choices.",
                            )
                        else:
                            noun = "observation" if len(rows) == 1 else "observations"
                            messages.success(
                                request,
                                f"Imported {len(rows)} vehicle location {noun}.",
                            )
                            return redirect(
                                "evidence:vehicle_import_review",
                                case_id=case.pk,
                                import_id=batch.pk,
                            )
    observations = (
        batch.observations.filter(case=case, vehicle__case_id=F("case_id"))
        .select_related("vehicle")
        .order_by("source_row")
        if batch.completed_at
        else []
    )
    return render(
        request,
        "evidence/import_review.html",
        {
            **case_context(case, request.user),
            "batch": batch,
            "form": form,
            "rows": rows,
            "invalid": any(row.errors for row in rows),
            "observations": observations,
            "error_message": error_message,
            "nav_section": "evidence",
        },
        status=status,
    )

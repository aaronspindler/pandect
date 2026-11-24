"""Case-authorized investigator workflows for retained original evidence."""

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import (
    require_http_methods,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required, wants_json
from core.analysis.automatic import start_added_evidence_analysis
from core.analysis.errors import AnalysisError
from core.evidence.navigation import query_suffix
from core.evidence.queries import get_evidence
from core.evidence.types import (
    CALL_KINDS,
)
from core.evidence.views import (
    SAVE_UNCONFIRMED,
    _detail_url,
    _error,
    _file_download,
)
from core.storage import retain_uploaded_originals, staged_file_upload
from phone.forms import CallAttachmentForm, call_content_matches, save_call_content
from phone.models import CallEvidenceAttachment


def _archived_call(request):
    return _error(
        request,
        "source_archived",
        "Restore this call before adding transcript or audio.",
        409,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def add_attachment(request, case_id, kind, evidence_id):
    if kind not in CALL_KINDS:
        raise Http404("Call not found.")
    status = 200
    form = attachment = None
    is_post = request.method == "POST"
    try:
        files = None
        if is_post:
            case = get_case(request.user, case_id, edit=True)
            item = get_evidence(case, kind, evidence_id)
            if item.is_archived:
                return _archived_call(request)
            files = request.FILES
            if getattr(request, "evidence_upload_too_large", False):
                return _error(
                    request,
                    "file_too_large",
                    "The file exceeds the configured upload limit.",
                    413,
                )
            if sum(len(files.getlist(key)) for key in files) > 1:
                return _error(request, "too_many_files", "Send one file at a time.")
        with (
            staged_file_upload(
                files.get("file") if files is not None else None,
                limit=settings.PANDECT_MAX_FILE_BYTES,
            ),
            retain_uploaded_originals() as originals,
        ):
            case = get_case(request.user, case_id, edit=True, lock=is_post)
            item = get_evidence(case, kind, evidence_id, lock=is_post)
            if item.is_archived:
                return _archived_call(request)
            reader_url = _detail_url(case, kind, item) + query_suffix(request)
            form = CallAttachmentForm(request.POST if is_post else None, files)
            if is_post:
                if form.is_valid():
                    submission_id = form.cleaned_data["submission_id"]
                    previous = CallEvidenceAttachment.objects.filter(
                        submission_id=submission_id
                    ).first()
                    if previous and (
                        previous.case_id != case.pk
                        or previous.uploaded_by_id != request.user.pk
                        or previous.phone_call_id
                        != (item.pk if kind == "phone-call" else None)
                        or previous.facetime_id
                        != (item.pk if kind == "facetime" else None)
                        or not call_content_matches(previous, form.cleaned_data)
                    ):
                        return _error(
                            request,
                            "submission_conflict",
                            "This submission cannot be reused.",
                            409,
                        )
                    attachment = previous
                    if attachment is None:
                        attachment = save_call_content(
                            item, request.user, form.cleaned_data, submission_id
                        )
                        if attachment.file:
                            originals.append(attachment.file)
                        start_added_evidence_analysis(
                            request.user,
                            case.pk,
                            kind,
                            item.pk,
                            attachment_id=attachment.pk,
                        )
                else:
                    status = 400
    except OSError:
        if form is None or not form.is_bound:
            return _error(
                request,
                "storage_unavailable",
                "The original could not be read. Keep your file and try again.",
                503,
            )
        form.add_error(
            None, "The original could not be saved. Keep your file and try again."
        )
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
        if wants_json(request) or form is None or not form.is_bound:
            return _error(request, "save_unconfirmed", SAVE_UNCONFIRMED, 503)
        form.add_error(None, SAVE_UNCONFIRMED)
        status = 503
    else:
        if attachment is not None:
            url = reader_url + f"#attachment-{attachment.pk}"
            if wants_json(request):
                return JsonResponse(
                    {"id": str(attachment.pk), "detail_url": url},
                    status=200 if previous else 201,
                )
            messages.success(
                request,
                "This attachment was already saved."
                if previous
                else "Transcript or audio added. Earlier material is retained.",
            )
            return redirect(url)
    if is_post and wants_json(request):
        return _error(
            request,
            "storage_unavailable" if status == 503 else "validation_error",
            "Review the highlighted fields.",
            status,
            form.errors.get_json_data(),
        )
    return render(
        request,
        "phone/attachment_form.html",
        case_context(case, request.user)
        | {
            "item": item,
            "reader_url": reader_url,
            "form": form,
            "max_text_bytes": settings.PANDECT_MAX_TEXT_BYTES,
            "max_file_bytes": settings.PANDECT_MAX_FILE_BYTES,
            "nav_section": "evidence",
        },
        status=status,
    )


@product_login_required
@require_safe
def attachment_download(request, case_id, kind, evidence_id, attachment_id, component):
    if kind not in CALL_KINDS:
        raise Http404("Attachment not found.")
    case = get_case(request.user, case_id)
    item = get_evidence(case, kind, evidence_id)
    try:
        attachment = item.attachments.get(pk=attachment_id, case=case)
    except CallEvidenceAttachment.DoesNotExist:
        raise Http404("Attachment not found.") from None
    if component == "manifest":
        response = HttpResponse(
            attachment.original_bytes(), content_type="application/octet-stream"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="attachment-{attachment.pk}.json"'
        )
    elif component == "transcript" and attachment.transcript:
        response = HttpResponse(
            attachment.transcript.encode("utf-8"),
            content_type="application/octet-stream",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="transcript-{attachment.pk}.txt"'
        )
    elif component == "audio" and attachment.file:
        response = _file_download(request, case, kind, item, attachment=attachment)
    else:
        raise Http404("Attachment not found.")
    response["X-Content-Type-Options"] = "nosniff"
    return response

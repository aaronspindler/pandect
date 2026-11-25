"""HTTP responses shared by case evidence and call attachment routes."""

from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.text import get_valid_filename

from cases.permissions import case_context
from config.access import wants_json
from core.storage import private_file

SAVE_UNCONFIRMED = (
    "The save could not be confirmed. Keep your original and retry this submission."
)


def error_response(request, code, message, status=400, errors=None):
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


def detail_url(case, kind, item):
    return reverse(
        "core:evidence:detail",
        kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
    )


def file_download(request, case, kind, item, *, attachment=None):
    source = attachment if attachment is not None else item
    try:
        original = private_file(source, case.pk).open("rb")
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

"""Case-authorized original WAV playback with bounded single-range seeking."""

import re

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_safe

from audio.previews import inspect_recording, playback_message
from cases.permissions import get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError
from core.evidence.queries import get_evidence
from core.evidence.types import CALL_KINDS
from phone.models import CallEvidenceAttachment


def _response(request, item, *, kind="audio", attachment=None):
    try:
        content, checksum, _ = inspect_recording(item, kind=kind, attachment=attachment)
    except AnalysisError as error:
        response = HttpResponse(
            playback_message(error), status=422, content_type="text/plain"
        )
    else:
        total = len(content)
        etag = f'"{checksum}"'
        start, end, status = 0, total - 1, 200
        requested = request.headers.get("Range", "")
        if requested and request.headers.get("If-Range", etag) == etag:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()) or len(requested) > 80:
                status = 416
            else:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), end) if last else end
                else:
                    start = max(0, total - int(last))
                status = 206 if 0 <= start <= end < total else 416
        response = HttpResponse(
            content[start : end + 1]
            if status != 416 and request.method != "HEAD"
            else b"",
            status=status,
            content_type="audio/wav",
        )
        response["Accept-Ranges"] = "bytes"
        response["ETag"] = etag
        response["Content-Disposition"] = 'inline; filename="recording.wav"'
        response["Content-Length"] = end - start + 1 if status != 416 else 0
        if status == 206:
            response["Content-Range"] = f"bytes {start}-{end}/{total}"
        elif status == 416:
            response["Content-Range"] = f"bytes */{total}"
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@product_login_required
@require_safe
def play(request, case_id, evidence_id):
    case = get_case(request.user, case_id)
    item = get_evidence(case, "audio", evidence_id)
    return _response(request, item)


@product_login_required
@require_safe
def play_attachment(request, case_id, kind, evidence_id, attachment_id):
    case = get_case(request.user, case_id)
    if kind not in CALL_KINDS:
        raise Http404
    item = get_evidence(case, kind, evidence_id)
    parent = "phone_call" if kind == "phone-call" else "facetime"
    attachment = get_object_or_404(
        CallEvidenceAttachment, case=case, pk=attachment_id, **{parent: item}
    )
    if not attachment.file:
        raise Http404
    return _response(request, item, kind=kind, attachment=attachment)

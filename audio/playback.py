"""Serve validated original recording bytes to authorized readers."""

import re

from django.http import HttpResponse

from audio.previews import inspect_recording, playback_message
from core.analysis.errors import AnalysisError


def playback_response(request, item, *, kind="audio", attachment=None):
    try:
        content, checksum, parsed = inspect_recording(
            item, kind=kind, attachment=attachment
        )
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
            content_type=parsed["content_type"],
        )
        response["Accept-Ranges"] = "bytes"
        response["ETag"] = etag
        extension = "m4a" if parsed["format"] == "M4A" else "wav"
        response["Content-Disposition"] = f'inline; filename="recording.{extension}"'
        response["Content-Length"] = end - start + 1 if status != 416 else 0
        if status == 206:
            response["Content-Range"] = f"bytes {start}-{end}/{total}"
        elif status == 416:
            response["Content-Range"] = f"bytes */{total}"
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response

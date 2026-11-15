"""Private, bounded PDF page images."""

import base64

from django.http import HttpResponse
from django.views.decorators.http import require_safe

from cases.permissions import get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError
from core.evidence.queries import get_evidence
from document.previews import inspect_document, preview_message


@product_login_required
@require_safe
def page(request, case_id, evidence_id, page):
    case = get_case(request.user, case_id)
    item = get_evidence(case, "document", evidence_id)
    try:
        parsed = inspect_document(item, page=page, render=True)
        if parsed["format"] != "PDF":
            raise AnalysisError("unsupported_document_format")
        content = base64.b64decode(parsed["image"], validate=True)
    except AnalysisError as error:
        response = HttpResponse(
            preview_message(error), status=422, content_type="text/plain"
        )
    else:
        response = HttpResponse(content, content_type="image/jpeg")
        response["Content-Disposition"] = f'inline; filename="page-{page}.jpg"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response

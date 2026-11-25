"""Case-authorized original recording playback."""

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_safe

from audio.playback import playback_response
from cases.permissions import get_case
from config.access import product_login_required
from core.evidence.queries import get_evidence
from core.evidence.types import CALL_KINDS
from phone.models import CallEvidenceAttachment


@product_login_required
@require_safe
def play(request, case_id, evidence_id):
    case = get_case(request.user, case_id)
    item = get_evidence(case, "audio", evidence_id)
    return playback_response(request, item)


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
    return playback_response(request, item, kind=kind, attachment=attachment)

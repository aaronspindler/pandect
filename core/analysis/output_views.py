"""Deliver authorized saved result downloads, recordings and artifacts."""

import hashlib
import json

from django.http import Http404, HttpResponse
from django.views.decorators.http import require_safe

from audio.playback import playback_response
from cases.permissions import get_case
from config.access import product_login_required
from core.analysis.artifacts import MAX_ARTIFACT_BYTES
from core.analysis.errors import AnalysisError
from core.analysis.services import case_run
from core.analysis.source_resolution import resolve_active_inputs
from core.evidence.time_claims import unavailable_time_claim
from core.models import AnalysisArtifact, AnalysisResult
from core.storage import private_file
from text.analysis.attributed_sources import verify_saved_membership, writing_scope


@product_login_required
@require_safe
def download(request, case_id, run_id):
    case = get_case(request.user, case_id)
    run = case_run(case, run_id)
    try:
        result = AnalysisResult.objects.get(case=case, run=run)
        if scope := writing_scope(run.operation, run.parameters):
            verify_saved_membership(case, scope, run.inputs, result.pk)
            sources = ()
        else:
            sources = resolve_active_inputs(run, case, require_current_review=False)
    except AnalysisError, AnalysisResult.DoesNotExist:
        raise Http404("Result not found.") from None
    source_map = {source.id: source for source in sources}
    if any(
        unavailable_time_claim(run.operation, item, source_map)
        for item in result.data.get("items", [])
    ):
        raise Http404("A cited passage is unavailable.")
    data = {
        "case_id": str(case.pk),
        "run_id": str(run.pk),
        "operation": run.operation,
        "inputs": run.inputs,
        "parameters": run.parameters,
        "result": result.data,
        "provenance": result.provenance,
    }
    # Linux WebKit can render JSON attachments inline; use a download MIME type.
    response = HttpResponse(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/octet-stream",
    )
    response["Content-Disposition"] = f'attachment; filename="analysis-{run.pk}.json"'
    return response


@product_login_required
@require_safe
def audio(request, case_id, run_id, source_id):
    case = get_case(request.user, case_id)
    run = case_run(case, run_id)
    try:
        source = next(
            x
            for x in resolve_active_inputs(run, case, require_current_review=False)
            if x.id == str(source_id)
        )
    except AnalysisError, StopIteration:
        raise Http404("Recording not found.") from None
    file = (
        source.attachment.file
        if source.attachment
        else getattr(source.item, "file", None)
    )
    if not file or (source.kind != "audio" and source.attachment is None):
        raise Http404("Recording not found.")
    return playback_response(
        request, source.item, kind=source.kind, attachment=source.attachment
    )


@product_login_required
@require_safe
def artifact(request, case_id, run_id, artifact_id):
    case = get_case(request.user, case_id)
    run = case_run(case, run_id)
    try:
        resolve_active_inputs(run, case, require_current_review=False)
        retained = AnalysisArtifact.objects.get(
            pk=artifact_id, case=case, result__case=case, result__run=run
        )
        with private_file(retained, case.pk).open("rb") as original:
            content = original.read(MAX_ARTIFACT_BYTES + 1)
        if (
            str(retained.source_id) not in {x["id"] for x in run.inputs}
            or len(content) > MAX_ARTIFACT_BYTES
            or hashlib.sha256(content).hexdigest() != retained.sha256
            or retained.kind not in AnalysisArtifact.Kind.values
        ):
            raise Http404("Artifact unavailable.")
    except AnalysisArtifact.DoesNotExist, AnalysisError, OSError:
        raise Http404("Artifact unavailable.") from None
    reference = retained.kind == AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE
    response = HttpResponse(
        content, content_type="application/octet-stream" if reference else "image/jpeg"
    )
    response["Content-Disposition"] = (
        f'attachment; filename="reference-{retained.pk}.json"'
        if reference
        else f'inline; filename="preview-{retained.pk}.jpg"'
    )
    return response

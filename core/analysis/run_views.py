"""Open a saved analysis run and handle its lifecycle and estimate actions."""

from urllib.parse import urlencode
from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import (
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.address_estimates import (
    require_current_geocode_reviews,
    set_address_estimate,
)
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import (
    execution_destination,
    requires_inference,
)
from core.analysis.important import retained_finding_url
from core.analysis.navigation import (
    evidence_return_url,
    finding_url,
    findings_list_query,
    history_query,
    processing_back_query,
    query_suffix,
    result_navigation,
    run_url,
)
from core.analysis.operations import OPERATIONS
from core.analysis.presentation import measurement_formatter
from core.analysis.result_reader import retained_result_context
from core.analysis.retries import acknowledge_failure, retry_analysis, retry_route
from core.analysis.services import cancel_run, case_run
from core.analysis.source_resolution import resolve_active_inputs
from core.important.navigation import return_params
from core.models import AnalysisRun
from geo.analysis import presentation as geo_presentation
from people.models import Person
from ui.live import render_live


@product_login_required
@require_safe
def detail(request, case_id, run_id):
    case = get_case(request.user, case_id)
    run = case_run(case, run_id)
    source_error = None
    source_review_changed = False
    try:
        sources = resolve_active_inputs(run, case, require_current_review=False)
        try:
            require_current_geocode_reviews(run, sources)
        except AnalysisError as error:
            if error.code != "geocode_review_required":
                raise
            source_review_changed = True
    except (AnalysisError, Http404) as error:
        source_error = error
        sources = []
    if (
        source_error is None
        and run.operation == "sighting_group"
        and run.status == AnalysisRun.Status.COMPLETE
    ):
        if request.headers.get("X-Pandect-Live") == "1":
            return render_live(
                request, "core/analysis/sighting_ready.html", {"case": case, "run": run}
            )
        return redirect(
            retained_finding_url(case.pk, run, 0, history=return_params(request.GET))
        )
    history = history_query(request.GET)
    history_back = processing_back_query(history, batch_id=run.batch_id)
    back_to_batch = "batch" in history_back
    if not back_to_batch:
        history.pop("batch", None)
    reader_url = evidence_return_url(case.pk, request.GET)
    if (
        source_error is None
        and run.operation == "imessage_import"
        and not reader_url
        and request.GET.get("processing") != "1"
    ):
        return redirect(
            reverse("phone:imessage_review", args=[case.pk, run.pk], query=history)
        )
    findings_back = findings_list_query(request.GET)
    if findings_back is not None:
        history["return_findings"] = urlencode(findings_back)
    if reader_url:
        history["return_evidence"] = request.GET["return_evidence"]
        if back_to_batch:
            history_back["return_evidence"] = history["return_evidence"]
    finding_history = history | return_params(request.GET)
    back_to_analysis = (
        reverse("core:analysis:processing", args=[case.pk])
        + query_suffix(history_back)
        + ("#processing-history" if history_back else "")
    )
    back_label = "Back to batch checks" if back_to_batch else "Back to Processing"
    section = "processing"
    if reader_url and not back_to_batch:
        back_label = "Back to evidence"
        back_to_analysis = reader_url
        section = "evidence"
    elif findings_back is not None and not reader_url:
        back_label = "Back to findings"
        back_to_analysis = reverse(
            "core:analysis:browse", args=[case.pk], query=findings_back
        )
        section = "analysis"
    context = case_context(case, request.user) | {
        "nav_section": section,
        "run": run,
        "history_suffix": query_suffix(finding_history),
        "back_to_analysis": back_to_analysis,
        "back_label": back_label,
        "operation_label": (
            OPERATIONS[run.operation].label
            if run.operation in OPERATIONS
            else "Retained analysis"
        ),
        "retry_submission_id": uuid4(),
        "error_message": error_message(run.error_code),
        "processing": _processing_details(run),
        "source_unavailable": source_error is not None,
        "source_archived": isinstance(source_error, AnalysisError)
        and source_error.code == "source_archived",
        "source_review_changed": source_review_changed,
        "sources": sources,
    }
    if run.operation in {"style_profiles", "style_change"} and (
        scope := run.parameters.get("writing_scope")
    ):
        if person := Person.objects.filter(case=case, pk=scope["person_id"]).first():
            context["writing_person"] = person
            context["writing_url"] = reverse(
                "people:writing_style",
                args=[case.pk, person.pk],
                query={"mode": "compare"} if run.operation == "style_change" else None,
            )
    evidence_keys = {(source.kind, source.item.pk) for source in sources}
    if len(evidence_keys) == 1:
        kind, evidence_id = next(iter(evidence_keys))
        context["evidence_analysis_url"] = (
            reverse("core:evidence:detail", args=[case.pk, kind, evidence_id])
            + "#evidence-findings-heading"
        )
    assumptions = {
        key: value
        for key, value in run.parameters.items()
        if not key.startswith("_")
        and not context.get("source_unavailable")
        and run.operation != "journey_claims"
    }
    context["assumptions"] = measurement_formatter(
        assumptions, case=case, sources=sources, query=request.GET
    )(assumptions)
    if source_error is None:
        context.update(
            retained_result_context(
                request,
                case,
                run,
                sources,
                history=history,
                finding_history=finding_history,
            )
        )
    if (
        settings.PANDECT_MAP_TILE_URL
        and not context["source_unavailable"]
        and run.operation in geo_presentation.PRIMARY_FIELDS
        and (
            context.get("position_map_available") or run.status in ("queued", "running")
        )
    ):
        context.update(
            observation_tile_url=settings.PANDECT_MAP_TILE_URL,
            observation_tile_attribution=settings.PANDECT_MAP_TILE_ATTRIBUTION,
        )
        request.allow_observation_map_tiles = True
    context["can_retry"] = run.operation in OPERATIONS and run.status in {
        "failed",
        "cancelled",
        "partial",
        "unsupported",
        "insufficient",
        "empty",
        "complete",
    }
    if context["source_archived"]:
        context["can_retry"] = False
    elif context["can_edit"] and context["can_retry"]:
        try:
            retry_route(case, run)
        except AnalysisError:
            context["can_retry"] = False
            context["retry_blocked"] = "The service for this run is unavailable."
    return render_live(request, "core/analysis/detail.html", context)


@product_login_required
@require_POST
def cancel(request, case_id, run_id):
    run = cancel_run(request.user, case_id, run_id)
    messages.success(
        request,
        (
            "Processing cancelled. Already dispatched external requests cannot be "
            "recalled; their results will not be published."
        ),
    )
    return redirect(
        run_url(run.case_id, run.pk)
        + query_suffix(result_navigation(request.GET, case_id))
    )


@product_login_required
@require_POST
def acknowledge(request, case_id, run_id):
    run = acknowledge_failure(request.user, case_id, run_id)
    messages.success(
        request, "Failure acknowledged. The missing coverage remains recorded."
    )
    return redirect(
        run_url(case_id, run.pk) + query_suffix(result_navigation(request.GET, case_id))
    )


@product_login_required
@require_POST
def retry(request, case_id, run_id):
    run, created, issue = retry_analysis(
        request.user, case_id, run_id, submission_id=request.POST.get("submission_id")
    )
    if issue:
        reason = (
            "The inputs changed or are no longer available. Start a new analysis "
            "and review its source selection."
        )
        if issue == "prior_run_active":
            reason = (
                "Cancel the earlier run before starting another attempt. Confirm a "
                "stopped worker before recovering abandoned work."
            )
        elif issue in {"original_route_unavailable", "source_archived"}:
            reason = error_message(issue)
        messages.error(request, reason)
    elif created:
        messages.success(
            request, "A new run is queued; the earlier history is retained."
        )
    return redirect(
        run_url(run.case_id, run.pk)
        + query_suffix(result_navigation(request.GET, case_id))
    )


@product_login_required
@require_POST
def select_estimate(request, case_id, run_id, item_index):
    action = request.POST.get("action")
    if action not in {"use", "stop"}:
        return HttpResponse("Choose whether to use this estimate.", status=400)
    try:
        set_address_estimate(
            request.user, case_id, run_id, item_index, use=action == "use"
        )
    except AnalysisError, ValidationError:
        messages.error(
            request,
            "The sources changed or are unavailable. The estimate was not updated.",
        )
    else:
        messages.success(
            request,
            "Address estimate selected."
            if action == "use"
            else "Address estimate cleared.",
        )
    return redirect(
        finding_url(
            case_id, run_id, item_index, history=result_navigation(request.GET, case_id)
        )
    )


def _processing_details(run):
    execution = run.parameters.get("_execution", {})
    if not isinstance(execution, dict):
        execution = {}
    if execution:
        label = (
            "External processing"
            if execution.get("location") == "external"
            else "Local processing"
        )
    else:
        label = (
            "External processing approved"
            if run.external_processing
            else "Local processing"
        )
    return {
        "label": label,
        "legacy": not execution
        and bool(
            run.external_processing or requires_inference(run.operation, run.parameters)
        ),
        "model": execution.get("model", ""),
        "runtime": execution.get("runtime", ""),
        "destination": execution_destination(execution)
        if execution.get("location") == "external" and "backend" in execution
        else "",
    }

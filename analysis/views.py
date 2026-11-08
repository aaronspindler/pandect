"""Ordinary case-authorized analysis and source inspection workflows."""

import hashlib
import json
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required
from evidence.navigation import query_suffix
from evidence.queries import get_evidence, scoped_evidence
from evidence.types import KIND_LABELS, KINDS

from .artifacts import MAX_ARTIFACT_BYTES
from .automatic import evidence_runs, start_evidence_analysis
from .errors import AnalysisError, error_message
from .execution import requires_inference
from .forms import AnalysisForm, EvidenceAnalysisForm, ReportForm, ReviewForm, selection
from .models import AnalysisArtifact, AnalysisResult, AnalysisReview, AnalysisRun
from .navigation import FINDINGS_PER_PAGE, finding_url
from .operations import INPUT_GUIDANCE, OPERATIONS, get_operation
from .presentation import measurement_formatter
from .reports import report
from .services import (
    cancel_run,
    require_current_geocode_reviews,
    resolve_inputs,
    review_result,
    start_run,
)


def private(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _context(request, case):
    return case_context(case, request.user) | {"nav_section": "analysis"}


def _run(case, run_id):
    try:
        return (
            AnalysisRun.objects.select_related("requested_by", "previous_run")
            .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
            .get(case=case, pk=run_id)
        )
    except AnalysisRun.DoesNotExist:
        raise Http404("Processing not found.") from None


def _url(case, run):
    return reverse("analysis:detail", kwargs={"case_id": case.pk, "run_id": run.pk})


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
        "recorded": bool(execution),
        "legacy": not execution
        and bool(
            run.external_processing or requires_inference(run.operation, run.parameters)
        ),
        "model": execution.get("model", ""),
        "runtime": execution.get("runtime", ""),
    }


def _additional_operations(kind):
    return {
        key: spec
        for key, spec in OPERATIONS.items()
        if kind in spec.kinds and (spec.external or key == "text_topics")
    }


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    page = Paginator(
        AnalysisRun.objects.filter(case=case).select_related("requested_by"), 25
    ).get_page(request.GET.get("page"))
    titles = {}
    for kind in KINDS:
        ids = {
            run.inputs[0]["evidence_id"]
            for run in page
            if run.inputs and run.inputs[0]["kind"] == kind
        }
        if ids:
            titles.update(
                ((kind, str(pk)), title)
                for pk, title in scoped_evidence(case, kind)
                .filter(pk__in=ids)
                .values_list("pk", "title")
            )
    for run in page:
        run.label = get_operation(run.operation).label
        run.processing_label = _processing_details(run)["label"]
        first = run.inputs[0] if run.inputs else {}
        run.source_title = titles.get(
            (first.get("kind"), first.get("evidence_id")), "Source unavailable"
        )
        run.additional_sources = max(0, len(run.inputs) - 1)
    return private(
        render(
            request,
            "analysis/index.html",
            _context(request, case)
            | {
                "page_obj": page,
                "reports": (
                    ("calls", "Call patterns", "Counts, duration and timing"),
                    (
                        "vehicle-coverage",
                        "Vehicle observation coverage",
                        "Recorded locations and gaps",
                    ),
                    (
                        "vehicle-speed",
                        "Vehicle speed and heading",
                        "Source-reported movement",
                    ),
                    (
                        "duplicates",
                        "Exact file duplicates",
                        "Files with identical bytes",
                    ),
                ),
            },
        )
    )


@product_login_required
@require_safe
def current_report(request, case_id, name):
    case = get_case(request.user, case_id)
    if name not in {"calls", "vehicle-coverage", "vehicle-speed", "duplicates"}:
        raise Http404("Report not found.")
    form = ReportForm(request.GET or None, case=case, report_name=name)
    filters_applied = request.GET.get("state", "active") != "active" or any(
        request.GET.get(field)
        for field in (
            "start",
            "end",
            "vehicle_id",
            "participant_id",
            "other_participant_id",
        )
    )
    data = None
    if not form.is_bound or form.is_valid():
        values = form.cleaned_data if form.is_bound else {"state": "active"}
        try:
            data = report(case, name, **{k: v or None for k, v in values.items()})
        except ValidationError as error:
            form.add_error(None, error)
    if data is not None:
        formatter = measurement_formatter(data, case=case)
        data["measurements"] = formatter(data.get("metrics", {}))
        for item in data.get("items", []):
            item["measurements"] = formatter(
                item.get("values", {}), duplicate_text=item.get("text", "")
            )
    return private(
        render(
            request,
            "analysis/report.html",
            _context(request, case)
            | {
                "form": form,
                "data": data,
                "name": name,
                "filters_applied": filters_applied,
            },
        )
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def process_evidence(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id, edit=request.method == "POST")
    item = get_evidence(case, kind, evidence_id)
    form = EvidenceAnalysisForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            runs, created = start_evidence_analysis(
                request.user,
                case.pk,
                kind,
                item.pk,
                submission_id=form.cleaned_data["submission_id"],
            )
        except (ValidationError, AnalysisError) as error:
            form.add_error(
                None,
                error
                if isinstance(error, ValidationError)
                else (
                    "A retained source is unavailable or changed. "
                    "Review the original and try again."
                ),
            )
        else:
            if not runs:
                messages.info(
                    request,
                    "No local analysis is available for this evidence’s "
                    "retained content.",
                )
            else:
                messages.success(
                    request,
                    "Local analysis queued. Refresh this page to follow its progress."
                    if created
                    else "This analysis was already queued.",
                )
            return redirect(
                reverse(
                    "analysis:process_evidence",
                    kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
                )
                + query_suffix(request)
            )
    runs = evidence_runs(case, kind, item.pk)
    local_runs = runs.filter(parameters__has_key="automatic_batch")
    local_analysis_pending = local_runs.filter(
        status__in=("queued", "running")
    ).exists()
    page = Paginator(runs, 25).get_page(request.GET.get("results_page"))
    for run in page:
        run.label = get_operation(run.operation).label
        run.processing_label = _processing_details(run)["label"]
    return private(
        render(
            request,
            "analysis/evidence.html",
            _context(request, case)
            | {
                "evidence": item,
                "kind": kind,
                "kind_label": KIND_LABELS[kind],
                "nav_section": "evidence",
                "list_suffix": query_suffix(request),
                "form": form,
                "page_obj": page,
                "local_analysis_pending": local_analysis_pending,
                "has_local_analysis": local_analysis_pending or local_runs.exists(),
                "additional_operations": _additional_operations(kind).items(),
            },
            status=400 if form.errors else 200,
        )
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def create(request, case_id, kind=None, evidence_id=None):
    case = get_case(request.user, case_id, edit=True)
    evidence = None
    evidence_url = ""
    operations = OPERATIONS
    if evidence_id is not None:
        item = get_evidence(case, kind, evidence_id)
        evidence = (kind, item)
        evidence_url = reverse(
            "evidence:detail",
            kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
        )
        operations = _additional_operations(kind)
    operation = (
        request.POST.get("operation")
        if request.method == "POST"
        else request.GET.get("operation", "native_text")
    )
    if operation not in operations:
        operation = next(iter(operations))
    query = request.GET.get("q", "")[:200] if evidence is None else ""
    form = AnalysisForm(
        request.POST if request.method == "POST" else None,
        case=case,
        operation=operation,
        query=query,
        evidence=evidence,
    )
    form.fields["operation"].choices = [
        (key, spec.label) for key, spec in operations.items()
    ]
    if request.method == "POST" and form.is_valid():
        try:
            run, created = start_run(
                request.user,
                case.pk,
                form.cleaned_data["operation"],
                [selection(x) for x in form.cleaned_data["sources"]],
                parameters=form.parameters(),
                submission_id=form.cleaned_data["submission_id"],
                external_processing=form.cleaned_data["external_processing"],
            )
        except (ValidationError, AnalysisError) as error:
            form.add_error(
                None,
                error
                if isinstance(error, ValidationError)
                else error_message(error.code),
            )
        else:
            messages.success(
                request,
                "Analysis queued." if created else "This analysis was already queued.",
            )
            return redirect(_url(case, run))
    return private(
        render(
            request,
            "analysis/create.html",
            _context(request, case)
            | {
                "form": form,
                "operations": operations.items(),
                "operation": operation,
                "operation_label": OPERATIONS[operation].label,
                "input_guidance": INPUT_GUIDANCE.get(operation, ""),
                "query": query,
                "evidence": evidence[1] if evidence else None,
                "evidence_url": evidence_url,
            },
        )
    )


def _items(data, sources, case, run, *, offset=0):
    source_map = {str(x.id): x for x in sources}
    formatter = measurement_formatter(data, case=case, sources=sources)
    artifacts = list(
        AnalysisArtifact.objects.filter(case=case, result__case=case, result__run=run)
    )
    previews = {
        str(x.source_id): x
        for x in artifacts
        if x.kind == AnalysisArtifact.Kind.IMAGE_PREVIEW
    }
    references = {
        x.metadata.get("reference_key"): x
        for x in artifacts
        if x.kind == AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE
    }
    items = []
    latest_reviews = {
        reviewed.item_index: reviewed
        for reviewed in AnalysisReview.objects.filter(
            case=case,
            result__case=case,
            result__run=run,
            item_index__gte=offset,
            item_index__lt=offset + len(data.get("items", [])),
        )
        .order_by("item_index", "-created_at", "id")
        .distinct("item_index")
        .select_related("reviewed_by")
    }
    for index, finding in enumerate(data.get("items", [])):
        item = dict(finding)
        item["measurements"] = formatter(
            item.get("values", {}), duplicate_text=item.get("text", "")
        )
        item["number"] = index + 1 + offset
        item["index"] = index + offset
        item["latest_review"] = latest_reviews.get(item["index"])
        links = []
        for source_id in item.get("source_ids", []):
            source = source_map.get(source_id)
            if source is not None:
                anchors = [
                    x
                    for x in item.get("anchors", [])
                    if x.get("source_id") == source_id
                ]
                url = source.url
                display_anchors = list(anchors)
                for anchor in anchors:
                    if source.derived_run_id and anchor.get("extraction_ranges"):
                        first = anchor["extraction_ranges"][0]
                        finding = first["finding_index"]
                        url = finding_url(case.pk, source.derived_run_id, finding)
                        display_anchors.extend(
                            x
                            for part in anchor["extraction_ranges"]
                            for x in part.get("original_anchors", [])
                        )
                links.append(
                    {
                        "label": source.item.title,
                        "url": url,
                        "anchors": display_anchors,
                        "derived": bool(source.derived_result_id),
                    }
                )
                for anchor in anchors:
                    if "start_seconds" in anchor and (
                        source.kind == "audio" or source.attachment is not None
                    ):
                        item["audio_url"] = (
                            reverse(
                                "analysis:audio",
                                kwargs={
                                    "case_id": case.pk,
                                    "run_id": run.pk,
                                    "source_id": source_id,
                                },
                            )
                            + f"#t={anchor['start_seconds']},{anchor['end_seconds']}"
                        )
        item["sources"] = links
        reference = references.get(item.get("values", {}).get("Reference snapshot"))
        if reference is not None:
            item["reference_snapshot"] = reference
        for anchor in item.get("anchors", []):
            preview = previews.get(anchor.get("source_id"))
            if preview and "region" in anchor:
                left, top, right, bottom = anchor["region"]
                item["preview"] = {
                    "url": reverse(
                        "analysis:artifact",
                        kwargs={
                            "case_id": case.pk,
                            "run_id": run.pk,
                            "artifact_id": preview.pk,
                        },
                    ),
                    "width": preview.width,
                    "height": preview.height,
                    "x": left * preview.width,
                    "y": top * preview.height,
                    "region_width": (right - left) * preview.width,
                    "region_height": (bottom - top) * preview.height,
                }
                break
        items.append(item)
    return items


def _map_points(sources):
    points = [x for x in sources if x.coordinates is not None]
    if not points:
        return []
    latitudes = [x.coordinates[0] for x in points]
    longitudes = [x.coordinates[1] for x in points]
    low_lat, high_lat = min(latitudes), max(latitudes)
    low_lon, high_lon = min(longitudes), max(longitudes)
    return [
        {
            "x": 30
            + 540 * (s.coordinates[1] - low_lon) / max(high_lon - low_lon, 0.00001),
            "y": 270
            - 240 * (s.coordinates[0] - low_lat) / max(high_lat - low_lat, 0.00001),
            "label": s.item.title,
            "url": s.url,
            "derived": getattr(s.item, "latitude", None) is None,
        }
        for s in points
    ]


@product_login_required
@require_safe
def detail(request, case_id, run_id):
    case = get_case(request.user, case_id)
    run = _run(case, run_id)
    context = _context(request, case) | {
        "run": run,
        "operation_label": get_operation(run.operation).label,
        "retry_submission_id": uuid4(),
        "error_message": error_message(run.error_code),
        "processing": _processing_details(run),
    }
    try:
        sources = resolve_inputs(run, case, require_current_review=False)
        try:
            require_current_geocode_reviews(run, sources)
        except AnalysisError as error:
            if error.code == "geocode_review_required":
                context["source_review_changed"] = True
            else:
                raise
    except AnalysisError, Http404:
        context["source_unavailable"] = True
        sources = []
    context["sources"] = sources
    assumptions = {
        key: value for key, value in run.parameters.items() if key != "_execution"
    }
    context["assumptions"] = measurement_formatter(
        assumptions, case=case, sources=sources
    )(assumptions)
    context["map_points"] = _map_points(sources)
    if not context.get("source_unavailable"):
        result = AnalysisResult.objects.filter(
            case=case, run=run, run__case=case
        ).first()
        if result is not None:
            reviews = list(
                result.reviews.filter(case=case).select_related("reviewed_by")
            )
            finding_page = Paginator(
                result.data.get("items", []), FINDINGS_PER_PAGE
            ).get_page(request.GET.get("page"))
            context |= {
                "result": result,
                "data": result.data,
                "metrics": measurement_formatter(
                    result.data.get("metrics", {}), case=case, sources=sources
                )(result.data.get("metrics", {})),
                "items": _items(
                    result.data | {"items": list(finding_page)},
                    sources,
                    case,
                    run,
                    offset=(finding_page.number - 1) * FINDINGS_PER_PAGE,
                ),
                "finding_page": finding_page,
                "reviews": reviews,
                "latest_result_review": next(
                    (review for review in reviews if review.item_index is None), None
                ),
                "reference_snapshots": result.artifacts.filter(
                    case=case, kind=AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE
                ),
            }
    context["can_retry"] = run.status in {
        "failed",
        "cancelled",
        "partial",
        "unsupported",
        "insufficient",
        "empty",
        "complete",
    }
    return private(render(request, "analysis/detail.html", context))


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
    return redirect(_url(run.case, run))


@product_login_required
@require_POST
def retry(request, case_id, run_id):
    case = get_case(request.user, case_id, edit=True)
    prior = _run(case, run_id)
    if prior.status in {"queued", "running"}:
        messages.error(
            request,
            (
                "Cancel the earlier run before starting another attempt. Confirm a "
                "stopped worker before recovering abandoned work."
            ),
        )
        return redirect(_url(case, prior))
    choices = [
        {
            "kind": x["kind"],
            "id": x["evidence_id"],
            **{
                k: x[k]
                for k in ("attachment_id", "result_id", "geocode_candidate")
                if x.get(k) is not None
            },
        }
        for x in prior.inputs
    ]
    parameters = dict(prior.parameters)
    if (
        requires_inference(prior.operation, parameters)
        and "backend" not in parameters
        and prior.external_processing
    ):
        parameters["backend"] = "gateway"
    try:
        run, _ = start_run(
            request.user,
            case.pk,
            prior.operation,
            choices,
            parameters=parameters,
            external_processing=prior.external_processing,
            previous_run=prior,
            submission_id=request.POST.get("submission_id") or uuid4(),
        )
    except ValidationError, AnalysisError:
        messages.error(
            request,
            (
                "The inputs changed or are no longer available. Start a new analysis "
                "and review its source selection."
            ),
        )
        return redirect(_url(case, prior))
    messages.success(request, "A new run is queued; the earlier history is retained.")
    return redirect(_url(case, run))


@product_login_required
@require_POST
def review(request, case_id, run_id):
    case = get_case(request.user, case_id, edit=True)
    run = _run(case, run_id)
    try:
        result = AnalysisResult.objects.get(case=case, run=run)
    except AnalysisResult.DoesNotExist:
        raise Http404("Result not found.") from None
    form = ReviewForm(request.POST)
    if form.is_valid():
        try:
            review_result(
                request.user,
                case_id,
                result.pk,
                form.cleaned_data["decision"],
                form.cleaned_data["comment"],
                item_index=form.cleaned_data.get("item_index"),
            )
        except ValidationError as error:
            form.add_error(None, error)
        except AnalysisError:
            form.add_error(
                None,
                "A selected source is unavailable or changed. "
                "Your review has not been saved. Check the source before retrying.",
            )
        else:
            messages.success(
                request,
                "Review recorded. Original evidence and earlier reviews are retained.",
            )
            index = form.cleaned_data.get("item_index")
            return redirect(
                _url(case, run)
                if index is None
                else finding_url(case.pk, run.pk, index)
            )
    index = form.cleaned_data.get("item_index")
    valid_index = index is not None and index < len(result.data.get("items", []))
    return private(
        render(
            request,
            "analysis/review.html",
            _context(request, case)
            | {
                "form": form,
                "run": run,
                "finding_number": index + 1 if valid_index else None,
                "return_url": (
                    finding_url(case.pk, run.pk, index)
                    if valid_index
                    else _url(case, run)
                ),
            },
            status=400,
        )
    )


@product_login_required
@require_safe
def download(request, case_id, run_id):
    case = get_case(request.user, case_id)
    run = _run(case, run_id)
    try:
        resolve_inputs(run, case, require_current_review=False)
        result = AnalysisResult.objects.get(case=case, run=run)
    except AnalysisError, AnalysisResult.DoesNotExist:
        raise Http404("Result not found.") from None
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
    return private(response)


@product_login_required
@require_safe
def audio(request, case_id, run_id, source_id):
    case = get_case(request.user, case_id)
    run = _run(case, run_id)
    try:
        source = next(
            x
            for x in resolve_inputs(run, case, require_current_review=False)
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
    try:
        stream = file.open("rb")
        header = stream.read(12)
        stream.seek(0)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            stream.close()
            raise Http404("This recording cannot be played inline.")
    except OSError:
        raise Http404("Recording unavailable.") from None
    return private(FileResponse(stream, content_type="audio/wav", as_attachment=False))


@product_login_required
@require_safe
def artifact(request, case_id, run_id, artifact_id):
    case = get_case(request.user, case_id)
    run = _run(case, run_id)
    try:
        resolve_inputs(run, case, require_current_review=False)
        retained = AnalysisArtifact.objects.get(
            pk=artifact_id, case=case, result__case=case, result__run=run
        )
        with retained.file.open("rb") as original:
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
    return private(response)

"""Ordinary case-authorized analysis and source inspection workflows."""

import hashlib
import json
from urllib.parse import urlsplit
from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.artifacts import MAX_ARTIFACT_BYTES
from core.analysis.automatic import start_evidence_analysis
from core.analysis.bulk import start_case_analysis
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import (
    requires_inference,
    retry_execution_confirmation,
    snapshot_execution,
)
from core.analysis.findings import additional_operations, case_highlights, finding_row
from core.analysis.form_base import selection
from core.analysis.forms import (
    EvidenceAnalysisForm,
    ReviewForm,
    analysis_form,
    report_form,
)
from core.analysis.navigation import (
    FINDINGS_PER_PAGE,
    finding_url,
    history_filters,
    history_query,
)
from core.analysis.navigation import query_suffix as analysis_query_suffix
from core.analysis.operations import (
    INPUT_GUIDANCE,
    OPERATION_GROUPS,
    OPERATIONS,
    get_operation,
)
from core.analysis.presentation import (
    measurement_formatter,
    prioritize_measurements,
    table_rows,
)
from core.analysis.reports import REPORT_NAMES, report
from core.analysis.result_views import result_view
from core.analysis.services import (
    cancel_run,
    require_current_geocode_reviews,
    resolve_inputs,
    review_result,
    start_run,
)
from core.evidence.collection import collection_context
from core.evidence.navigation import list_params
from core.evidence.queries import get_evidence
from core.evidence.types import KIND_LABELS
from core.evidence.views import render_detail
from core.models import AnalysisArtifact, AnalysisResult, AnalysisRun
from geo.analysis import presentation as geo_presentation
from phone.analysis import presentation as phone_presentation


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
    return reverse(
        "core:analysis:detail", kwargs={"case_id": case.pk, "run_id": run.pk}
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
    }


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    evidence = collection_context(case, QueryDict())
    return render(
        request,
        "core/analysis/index.html",
        _context(request, case)
        | {
            "highlights": case_highlights(AnalysisRun.objects.filter(case=case), case)[
                :5
            ],
            "recent_evidence": evidence["rows"][:5],
            "evidence_count": evidence["page_obj"].paginator.count,
            "reports": (
                ("vehicle-trips", "Vehicle trips", "Possible journeys and stops"),
                ("calls", "Call patterns", "Who called whom, when and how often"),
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


@product_login_required
@require_POST
def rerun_all(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    form = EvidenceAnalysisForm(request.POST)
    if form.is_valid():
        try:
            _, created = start_case_analysis(
                request.user, case.pk, submission_id=form.cleaned_data["submission_id"]
            )
        except ValidationError:
            messages.error(
                request, "This request could not be used. Try rerunning analysis again."
            )
        except DatabaseError:
            messages.error(request, "Analysis could not be queued. Please try again.")
        else:
            messages.success(
                request,
                "Analysis requested for all active evidence. "
                "Refresh to follow its progress."
                if created
                else "This analysis request has already been queued.",
            )
    else:
        messages.error(request, "This request expired. Please try again.")
    return redirect("core:analysis:processing", case_id=case.pk)


@product_login_required
@require_safe
def current_report(request, case_id, name):
    case = get_case(request.user, case_id)
    if name not in REPORT_NAMES:
        raise Http404("Report not found.")
    filter_data = request.GET.copy()
    if filter_data:
        filter_data.setdefault("state", "active")
    form = report_form(filter_data or None, case=case, report_name=name)
    filters_applied = request.GET.get("state", "active") != "active" or any(
        request.GET.get(field)
        for field in (
            "start",
            "end",
            "vehicle_id",
            "participant_id",
            "other_participant_id",
            "duplicate_row",
        )
    )
    data = None
    if not form.is_bound or form.is_valid():
        values = form.cleaned_data if form.is_bound else {"state": "active"}
        try:
            data = report(
                case,
                name,
                page=request.GET.get("page", 1),
                gap_page=request.GET.get("gap_page", 1),
                pair_page=request.GET.get("pair_page", 1),
                duplicate_row=request.GET.get("duplicate_row") or None,
                **{k: v or None for k, v in values.items()},
            )
        except ValidationError as error:
            form.add_error(None, error)
    if data is not None:
        formatter = measurement_formatter(data, case=case, query=request.GET)
        data["measurements"] = formatter(data.get("metrics", {}))
        summary_fields = {
            "calls": (
                "Reported duration total (seconds, known only)",
                "Reported duration median (seconds, known only)",
                "Unknown duration",
                "Zero duration",
                "Duration distribution",
            ),
            "vehicle-coverage": (
                "Vehicles in selection",
                "Missing observation time in selection",
                "Intervals of at least 30 minutes",
                "Out-of-order CSV rows",
                "Negative receipt delays",
            ),
            "vehicle-speed": (
                "Known reported speeds",
                "Unknown reported speeds",
                "Zero reported speeds",
                "Reported speed median (km/h)",
                "Reported heading counts",
            ),
            "vehicle-trips": ("Possible trips", "Vehicles in selection"),
            "duplicates": ("Duplicate byte groups", "Occurrences in duplicate groups"),
        }
        prioritize_measurements(data, "Selected records", *summary_fields[name])
        for item in data["items"]:
            item["measurements"] = formatter(item.get("values", {}))
        if name == "calls":
            tables = phone_presentation.report_table(data)
        elif name == "vehicle-trips":
            tables = []
        elif name in {"vehicle-coverage", "vehicle-speed"}:
            tables = geo_presentation.report_table(data, name)
        else:
            grouped = {}
            for item in data["items"]:
                grouped.setdefault(item["values"]["SHA-256"], []).append(item)
            tables = [
                {
                    "title": f"Group {items[0]['values']['Group']}",
                    "checksum": checksum,
                    "items": items,
                    "columns": table_rows(
                        items,
                        (
                            ("Retained file", ("Source type", "Byte size")),
                            (
                                "Record history",
                                ("Uploaded at (UTC)", "Archived parent"),
                            ),
                        ),
                    ),
                }
                for checksum, items in grouped.items()
            ]
    else:
        tables = []
    return render(
        request,
        "core/analysis/report.html",
        _context(request, case)
        | {
            "form": form,
            "data": data,
            "name": name,
            "tables": tables,
            "filters_applied": filters_applied,
            "duplicate_row": request.GET.get("duplicate_row", ""),
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def process_evidence(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id, edit=request.method == "POST")
    item = get_evidence(case, kind, evidence_id)
    params = list_params(request)
    if request.method == "GET" and request.GET.get("results_page"):
        params["results_page"] = request.GET["results_page"]
    detail_url = (
        reverse("core:evidence:detail", args=[case.pk, kind, item.pk])
        + ("?" + params.urlencode() if params else "")
        + "#evidence-findings-heading"
    )
    if request.method == "GET":
        return redirect(detail_url)
    form = EvidenceAnalysisForm(request.POST)
    if form.is_valid():
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
            return redirect(detail_url)
    return render_detail(request, case, kind, item, analysis_form=form, status=400)


@product_login_required
@require_http_methods(["GET", "POST"])
def create(request, case_id, kind=None, evidence_id=None):
    case = get_case(request.user, case_id, edit=True)
    evidence = None
    evidence_url = ""
    focused_source = request.GET.get("source", "")
    operations = {
        name: spec
        for name, spec in OPERATIONS.items()
        if name not in {"question_synthesis", "imazing_import"}
    }
    if evidence_id is not None:
        item = get_evidence(case, kind, evidence_id)
        evidence = (kind, item)
        evidence_url = reverse(
            "core:evidence:detail",
            kwargs={"case_id": case.pk, "kind": kind, "evidence_id": item.pk},
        )
        operations = additional_operations(kind)
    elif focused_source:
        try:
            if (
                len(focused_source) > 500
                or not 2 <= len(focused_source.split(":")) <= 5
            ):
                raise ValueError
            focused = selection(focused_source)
            item = get_evidence(case, focused["kind"], focused["id"])
        except ValueError, IndexError, ValidationError:
            raise Http404("Source not found.") from None
        evidence = (focused["kind"], item)
        evidence_url = reverse(
            "core:evidence:detail", args=[case.pk, focused["kind"], item.pk]
        )
    chooser_context = _context(request, case) | {
        "operation_groups": [
            (
                group,
                [
                    {
                        "key": key,
                        "label": operations[key].label,
                        "description": description,
                    }
                    for key, description in choices.items()
                    if key in operations
                ],
            )
            for group, choices in OPERATION_GROUPS.items()
            if operations.keys() & choices.keys()
        ],
        "evidence": evidence[1] if evidence else None,
        "evidence_url": evidence_url,
        "chooser_url": request.path,
    }
    operation = (
        request.POST.get("operation")
        if request.method == "POST"
        else request.GET.get("operation")
    )
    if request.method == "GET" and operation not in operations:
        return render(request, "core/analysis/create.html", chooser_context)
    if operation not in operations:
        operation = next(iter(operations))
    posted = request.POST.copy() if request.method == "POST" else None
    browse = request.POST.get("browse_sources") if posted is not None else None
    if posted is not None and request.POST.get("remove_source"):
        posted.setlist(
            "sources",
            [
                value
                for value in posted.getlist("sources")
                if value != posted["remove_source"]
            ],
        )
        browse = "remove"
    if browse == "clear":
        posted["q"] = ""
    query = (
        (posted if posted is not None else request.GET).get("q", "")[:200]
        if evidence is None
        else ""
    )
    navigation = posted if posted is not None else request.GET
    try:
        sources_page = max(1, int(navigation.get("sources_page", "1")))
    except ValueError:
        sources_page = 1
    sources_cursor = navigation.get("sources_cursor", "")
    sources_reverse = navigation.get("sources_reverse") == "1"
    if browse in {"search", "clear"}:
        sources_page, sources_cursor, sources_reverse = 1, "", False
    elif browse in {"next", "previous"}:
        sources_reverse = browse == "previous"
        sources_cursor = navigation.get(
            "source_before" if sources_reverse else "source_after", ""
        )
        sources_page = max(1, sources_page + (-1 if sources_reverse else 1))
    form = analysis_form(
        posted,
        case=case,
        operation=operation,
        query=query,
        evidence=evidence,
        sources_page=sources_page,
        sources_cursor=sources_cursor,
        sources_reverse=sources_reverse,
        selected_sources=(focused_source,) if focused_source else (),
    )
    form.fields["operation"].choices = [
        (key, spec.label) for key, spec in operations.items()
    ]
    if focused_source:
        if focused_source not in dict(form.fields["sources"].choices):
            raise Http404("Source not available for this analysis.")
        if request.method == "GET":
            form.initial["sources"] = [focused_source]
    if (
        request.method == "POST"
        and not browse
        and not getattr(form, "is_mapping_edit", False)
        and form.is_valid()
    ):
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
    if browse:
        from django.forms.utils import ErrorDict

        form._errors = ErrorDict()
    return render(
        request,
        "core/analysis/create.html",
        chooser_context
        | {
            "form": form,
            "source_options": [
                (
                    choice,
                    str(choice.data["value"]).count(":") == 1,
                    form.readiness.get(str(choice.data["value"])),
                )
                for choice in form["sources"]
                if str(choice.data["value"]) in dict(form.source_page)
            ],
            "operation": operation,
            "operation_label": OPERATIONS[operation].label,
            "input_guidance": INPUT_GUIDANCE.get(operation, ""),
            "result_guidance": next(
                keys[operation]
                for keys in OPERATION_GROUPS.values()
                if operation in keys
            ),
            "query": query,
        },
    )


def _finding_detail(
    finding, row, source_map, case, run, formatter, previews, references, prior_runs
):
    item = finding | row
    item["prior_finding_links"] = [
        citation
        | {
            "url": finding_url(
                case.pk, prior_runs[citation["result_id"]], citation["item_index"]
            )
        }
        for citation in finding.get("finding_citations", [])
        if citation.get("result_id") in prior_runs
    ]
    values = item.get("values", {})
    item["measurements"] = formatter(values, duplicate_text=item.get("text", ""))
    item["plate_candidate"] = run.operation == "plate_ocr"
    for source_id in item.get("source_ids", []):
        prepared = previews.get((source_id, row["index"]))
        if prepared is not None:
            item["prepared_preview"] = _preview_details(prepared, case, run)
            break
    links = []
    for source_id in item.get("source_ids", []):
        source = source_map.get(source_id)
        if source is not None:
            anchors = [
                x for x in item.get("anchors", []) if x.get("source_id") == source_id
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
                    "id": source_id,
                    "label": source.item.title,
                    "kind_label": KIND_LABELS[source.kind],
                    "attachment_label": phone_presentation.addition_label(
                        source, run.operation
                    ),
                    "url": url,
                    "anchors": display_anchors,
                    "excerpt": _source_excerpt(source, anchors)
                    if run.operation
                    in {
                        "entities",
                        "copied_text",
                        "text_topics",
                        "content_events",
                        "session_summaries",
                        "conversation_link",
                    }
                    else "",
                    "reviewed_coordinates": source.derived_coordinates
                    if getattr(source.item, "latitude", None) is not None
                    else None,
                    "derived": bool(source.derived_result_id),
                    "derived_label": "Selected geographic estimate"
                    if source.derived_coordinates is not None
                    else "Exact selected extraction version",
                    "original_url": source.original_url
                    if source.derived_result_id
                    else None,
                    "coordinates": source.coordinates,
                    "coordinate_label": (
                        "Reviewed estimate"
                        if getattr(source.item, "latitude", None) is None
                        else "Reported coordinates"
                    ),
                    "observed_at": getattr(source.item, "observed_at", None),
                }
            )
            interval = next(
                (anchor for anchor in anchors if "start_seconds" in anchor), None
            )
            if (
                source.kind == "audio" or (source.attachment and source.attachment.file)
            ) and (interval is not None or "Decoded duration (seconds)" in values):
                item["recording_url"] = reverse(
                    "core:analysis:audio",
                    kwargs={
                        "case_id": case.pk,
                        "run_id": run.pk,
                        "source_id": source_id,
                    },
                )
                if interval is not None:
                    start, end = interval["start_seconds"], interval["end_seconds"]
                    item.update(
                        start_seconds=start,
                        end_seconds=end,
                        audio_url=f"{item['recording_url']}#t={start},{end}",
                    )
    item["sources"] = links
    reference_key = values.get("Reference snapshot")
    reference = (
        references.get(reference_key) if isinstance(reference_key, str) else None
    )
    if reference is not None:
        item["reference_snapshot"] = reference
    for anchor in item.get("anchors", []):
        preview = previews.get((anchor.get("source_id"), None))
        if preview and "region" in anchor:
            left, top, right, bottom = anchor["region"]
            item["preview"] = _preview_details(preview, case, run) | {
                "x": left * preview.width,
                "y": top * preview.height,
                "region_width": (right - left) * preview.width,
                "region_height": (bottom - top) * preview.height,
            }
            break
    return item


def _preview_details(artifact, case, run):
    return {
        "url": reverse("core:analysis:artifact", args=[case.pk, run.pk, artifact.pk]),
        "width": artifact.width,
        "height": artifact.height,
    }


def _source_excerpt(source, anchors):
    for anchor in anchors:
        start, end = anchor.get("start"), anchor.get("end")
        if (
            type(start) is int
            and type(end) is int
            and 0 <= start < end <= len(source.text)
        ):
            return source.text[start : min(end, start + 320)] + (
                "…" if end - start > 320 else ""
            )
    return ""


def _retry_parameters(run):
    parameters = run.parameters
    if (
        requires_inference(run.operation, parameters)
        and "backend" not in parameters
        and run.external_processing
    ):
        return parameters | {"backend": "gateway"}
    return parameters


def _retry_destination(execution):
    if execution["backend"] == "gateway":
        account, gateway, model = (
            execution["account"],
            execution["gateway"],
            execution["model"],
        )
        if account and gateway and model:
            return f"Cloudflare AI Gateway · {account} / {gateway} · {model}"
        return "Cloudflare AI Gateway (configuration required)"
    try:
        endpoint = urlsplit(execution["url"])
        if endpoint.scheme in {"http", "https"} and endpoint.hostname:
            # Disclose the destination origin without credentials or route details.
            return f"{endpoint.scheme}://{endpoint.netloc.rsplit('@', 1)[-1]}"
    except ValueError:
        pass
    return "the geographic lookup service (configuration required)"


@product_login_required
@require_safe
def detail(request, case_id, run_id):
    case = get_case(request.user, case_id)
    run = _run(case, run_id)
    if run.operation == "imazing_import" and request.GET.get("processing") != "1":
        return redirect("phone:imazing_review", case_id=case.pk, run_id=run.pk)
    history = history_query(request.GET)
    history_page = history.get("history_page")
    history_back = history_filters(history) | (
        {"page": history_page} if history_page else {}
    )
    context = _context(request, case) | {
        "nav_section": "processing",
        "run": run,
        "history_suffix": analysis_query_suffix(history),
        "back_to_analysis": reverse("core:analysis:processing", args=[case.pk])
        + analysis_query_suffix(history_back),
        "operation_label": get_operation(run.operation).label,
        "retry_submission_id": uuid4(),
        "error_message": error_message(run.error_code),
        "processing": _processing_details(run),
    }
    context["can_review_result"] = (
        context["can_edit"] and run.operation != "geocode_locations"
    )
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
    evidence_keys = {(source.kind, source.item.pk) for source in sources}
    if len(evidence_keys) == 1:
        kind, evidence_id = next(iter(evidence_keys))
        context["evidence_analysis_url"] = (
            reverse("core:evidence:detail", args=[case.pk, kind, evidence_id])
            + "#evidence-findings-heading"
        )
    assumptions = {
        key: value for key, value in run.parameters.items() if key != "_execution"
    }
    context["assumptions"] = measurement_formatter(
        assumptions, case=case, sources=sources, query=request.GET
    )(assumptions)
    if not context.get("source_unavailable"):
        result = AnalysisResult.objects.filter(
            case=case, run=run, run__case=case
        ).first()
        if result is not None:
            reviews = list(
                result.reviews.filter(case=case).select_related("reviewed_by")
            )
            indexed_findings = list(enumerate(result.data.get("items", [])))
            weak_indices = {
                index
                for index, finding in indexed_findings
                if run.operation == "plate_ocr"
                and finding.get("values", {}).get("Weak plate region") is True
            }
            try:
                selected_index = int(request.GET.get("finding", "")) - 1
            except ValueError:
                selected_index = -1
            show_weak = bool(weak_indices) and (
                request.GET.get("weak") == "1" or selected_index in weak_indices
            )
            visible_findings = [
                (index, finding)
                for index, finding in indexed_findings
                if show_weak or index not in weak_indices
            ]
            selected_position = next(
                (
                    position
                    for position, (index, _) in enumerate(visible_findings)
                    if index == selected_index
                ),
                None,
            )
            findings = Paginator(visible_findings, FINDINGS_PER_PAGE)
            finding_page = findings.get_page(
                selected_position // FINDINGS_PER_PAGE + 1
                if selected_position is not None
                else request.GET.get("page")
            )
            focused = selected_position is not None or findings.count == 1
            entries = finding_page.object_list
            if selected_position is not None:
                entries = [entries[selected_position % FINDINGS_PER_PAGE]]
            latest_reviews = {}
            for review in reviews:
                latest_reviews.setdefault(review.item_index, review)
            latest_result_review = latest_reviews.pop(None, None)
            source_map = {str(source.id): source for source in sources}
            formatter = measurement_formatter(
                [finding.get("values", {}) for _, finding in entries],
                case=case,
                sources=sources,
                query=request.GET,
            )
            artifacts = result.artifacts.filter(case=case)
            previews = (
                {
                    (
                        str(artifact.source_id),
                        artifact.metadata.get("finding_index"),
                    ): artifact
                    for artifact in artifacts.filter(
                        kind=AnalysisArtifact.Kind.IMAGE_PREVIEW
                    )
                }
                if run.operation in {"ocr", "plate_ocr"} or focused
                else {}
            )
            snapshots = list(
                artifacts.filter(kind=AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE)
            )
            references = {
                snapshot.metadata.get("reference_key"): snapshot
                for snapshot in snapshots
            }
            prior_runs = {
                str(pk): run_id
                for pk, run_id in AnalysisResult.objects.filter(
                    case=case,
                    run__case=case,
                    pk__in=[
                        ref["result_id"]
                        for ref in run.parameters.get("finding_refs", [])
                    ],
                ).values_list("pk", "run_id")
            }
            items = [
                _finding_detail(
                    finding,
                    finding_row(
                        finding,
                        index,
                        latest_reviews.get(index),
                        case,
                        run,
                        show_weak=show_weak,
                        history=history,
                    ),
                    source_map,
                    case,
                    run,
                    formatter,
                    previews,
                    references,
                    prior_runs,
                )
                for index, finding in entries
            ]
            groups, template = result_view(run.operation, items, formatter)
            run_query = history | ({"weak": 1} if show_weak else {})
            context |= {
                "result": result,
                "result_groups": groups,
                "result_template": template,
                "run_query": run_query,
                "run_suffix": analysis_query_suffix(run_query),
                "map_points": geo_presentation.map_points(run.operation, items)
                if run.operation in geo_presentation.PRIMARY_FIELDS
                else [],
                "data": result.data,
                "metrics": measurement_formatter(
                    result.data.get("metrics", {}),
                    case=case,
                    sources=sources,
                    query=request.GET,
                )(result.data.get("metrics", {})),
                "items": items,
                "focused_finding": focused,
                "show_result_overview": not focused or run.status != "complete",
                "metadata_result": run.operation
                in {"image_metadata", "audio_metadata"},
                "finding_page": finding_page,
                "reviews": reviews,
                "reviewed_count": sum(
                    index in latest_reviews for index, _ in visible_findings
                ),
                "show_weak_regions": show_weak,
                "weak_region_count": len(
                    {
                        str(finding.get("values", {}).get("Plate region number", index))
                        for index, finding in indexed_findings
                        if index in weak_indices
                    }
                ),
                "latest_result_review": latest_result_review,
                "reference_snapshots": snapshots,
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
    if context["can_edit"] and context["can_retry"]:
        execution = snapshot_execution(run.operation, _retry_parameters(run))
        if execution and execution["location"] == "external":
            context["retry_destination"] = _retry_destination(execution)
            context["retry_data"] = {
                "text": "selected evidence content",
                "transcription": "selected recordings",
                "geocode": "selected addresses or coordinates",
                "map_context": "coordinates and search areas",
            }[execution["purpose"]]
            context["retry_confirmation"] = retry_execution_confirmation(
                request.user.pk, run.pk, context["retry_submission_id"], execution
            )
    return render(request, "core/analysis/detail.html", context)


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
        _url(run.case, run) + analysis_query_suffix(history_query(request.GET))
    )


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
        return redirect(
            _url(case, prior) + analysis_query_suffix(history_query(request.GET))
        )
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
    try:
        run, _ = start_run(
            request.user,
            case.pk,
            prior.operation,
            choices,
            parameters=_retry_parameters(prior),
            external_processing=request.POST.get("external_processing") == "on",
            previous_run=prior,
            submission_id=request.POST.get("submission_id") or uuid4(),
            retry_confirmation=request.POST.get("retry_confirmation", ""),
        )
    except (ValidationError, AnalysisError) as error:
        messages.error(
            request,
            error_message(error.code)
            if isinstance(error, AnalysisError)
            and error.code == "retry_approval_required"
            else (
                "The inputs changed or are no longer available. Start a new analysis "
                "and review its source selection."
            ),
        )
        return redirect(
            _url(case, prior) + analysis_query_suffix(history_query(request.GET))
        )
    messages.success(request, "A new run is queued; the earlier history is retained.")
    return redirect(_url(case, run) + analysis_query_suffix(history_query(request.GET)))


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
    show_weak = request.GET.get("weak") == "1" and run.operation == "plate_ocr"
    history = history_query(request.GET)
    run_suffix = analysis_query_suffix(history | ({"weak": 1} if show_weak else {}))
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
                _url(case, run) + run_suffix
                if index is None
                else finding_url(
                    case.pk, run.pk, index, show_weak=show_weak, history=history
                )
            )
    index = form.cleaned_data.get("item_index")
    valid_index = index is not None and index < len(result.data.get("items", []))
    return render(
        request,
        "core/analysis/review.html",
        _context(request, case)
        | {
            "form": form,
            "run": run,
            "finding_number": index + 1 if valid_index else None,
            "return_url": (
                finding_url(
                    case.pk, run.pk, index, show_weak=show_weak, history=history
                )
                if valid_index
                else _url(case, run) + run_suffix
            ),
            "run_suffix": run_suffix,
            "operation_label": get_operation(run.operation).label,
        },
        status=400,
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
    return response


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
    return FileResponse(stream, content_type="audio/wav", as_attachment=False)


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
    return response

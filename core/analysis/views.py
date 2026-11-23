"""Ordinary case-authorized analysis and source inspection workflows."""

import hashlib
import json
from urllib.parse import urlencode
from uuid import UUID, uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from audio.views import playback_response
from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.artifacts import MAX_ARTIFACT_BYTES
from core.analysis.automatic import start_evidence_reprocessing
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import (
    execution_destination,
    requires_inference,
)
from core.analysis.findings import additional_operations, case_highlights, finding_row
from core.analysis.form_base import (
    CALL_RECORDING_OPERATIONS,
    EXTRACTIONS_BY_KIND,
    selection,
)
from core.analysis.forms import (
    EvidenceAnalysisForm,
    analysis_form,
    report_form,
)
from core.analysis.manual import start_selected_analysis
from core.analysis.navigation import (
    FINDINGS_PER_PAGE,
    evidence_return_url,
    finding_url,
    findings_list_query,
    history_query,
    processing_back_query,
    result_navigation,
)
from core.analysis.navigation import query_suffix as analysis_query_suffix
from core.analysis.operations import (
    INPUT_GUIDANCE,
    OPERATION_GROUPS,
    OPERATIONS,
)
from core.analysis.presentation import (
    measurement_formatter,
    prioritize_measurements,
)
from core.analysis.projection import is_timing_only
from core.analysis.ranking import rank_findings, review_context
from core.analysis.readiness import (
    COORDINATE_OPERATIONS,
    TEXT_OPERATIONS,
    source_readiness,
)
from core.analysis.reports import REPORT_NAMES, report
from core.analysis.reprocessing import start_reprocessing
from core.analysis.result_views import focused_finding_context, result_view
from core.analysis.retries import retry_route, retry_run
from core.analysis.services import (
    address_estimate,
    cancel_run,
    require_current_geocode_reviews,
    resolve_inputs,
    set_address_estimate,
)
from core.evidence.collection import collection_context
from core.evidence.navigation import list_params
from core.evidence.queries import get_evidence
from core.evidence.time_claims import unavailable_time_claim
from core.evidence.types import CALL_KINDS, KIND_LABELS
from core.evidence.views import render_detail
from core.models import AnalysisArtifact, AnalysisResult, AnalysisReview, AnalysisRun
from geo.analysis import presentation as geo_presentation
from image.analysis.presentation import (
    finding_images,
    previews_by_result,
    visible_structure,
)
from phone.analysis import presentation as phone_presentation
from phone.models import CommunicationContext
from socialmedia import instagram_connector
from socialmedia.models import InstagramMonitor
from ui.live import render_live


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
        "destination": execution_destination(execution)
        if execution.get("location") == "external" and "backend" in execution
        else "",
    }


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    evidence = collection_context(case, QueryDict())
    return render_live(
        request,
        "core/analysis/index.html",
        _context(request, case)
        | {
            "highlights": case_highlights(AnalysisRun.objects.filter(case=case), case)[
                :3
            ],
            "recent_evidence": evidence["rows"][:3],
            "show_instagram_monitors": instagram_connector.scheduling_available()
            or InstagramMonitor.objects.filter(case=case).exists(),
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
        collection=True,
    )


@product_login_required
@require_POST
def rerun_all(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    form = EvidenceAnalysisForm(request.POST)
    if form.is_valid():
        try:
            _, created = start_reprocessing(
                request.user,
                case.pk,
                submission_id=form.cleaned_data["submission_id"],
                kinds=list(KIND_LABELS),
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        except DatabaseError:
            messages.error(request, "Analysis could not be queued. Please try again.")
        else:
            messages.success(
                request,
                "Analysis requested for all active evidence. "
                "Follow its progress in Processing."
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
    selected_names = []
    if data is not None:
        if form.is_bound:
            selected_names = [
                dict(form.fields[field].choices)[form.cleaned_data[field]]
                for field in ("vehicle_id", "participant_id", "other_participant_id")
                if form.cleaned_data.get(field)
            ]
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
                    "title": (
                        f"Group {items[0]['values']['Group']} · "
                        f"{items[0]['values']['Group occurrences']} identical files"
                    ),
                    "checksum": checksum,
                    "items": items,
                }
                for checksum, items in grouped.items()
            ]
    else:
        tables = []
    return (render_live if name == "vehicle-trips" else render)(
        request,
        "core/analysis/report.html",
        _context(request, case)
        | {
            "form": form,
            "data": data,
            "name": name,
            "tables": tables,
            "filters_applied": filters_applied,
            "selected_names": selected_names,
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
            batch, created = start_evidence_reprocessing(
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
            messages.success(
                request,
                "Analysis requested."
                if created
                else "This analysis was already requested.",
            )
            return redirect(
                reverse(
                    "core:analysis:processing",
                    args=(case.pk,),
                    query={"batch": batch.pk},
                    fragment="processing-history",
                )
            )
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
        if name not in {"question_synthesis", "imessage_import", "sighting_group"}
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
    kind_filter = evidence[0] if evidence else request.GET.get("type", "")
    if kind_filter not in KIND_LABELS:
        kind_filter = ""
    if kind_filter:
        operations = {
            name: spec for name, spec in operations.items() if kind_filter in spec.kinds
        }
    chooser_params = {"source": focused_source} if focused_source else {}
    if kind_filter and not evidence:
        chooser_params["type"] = kind_filter
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
        "chooser_url": request.path + analysis_query_suffix(chooser_params),
        "chooser_kinds": KIND_LABELS.items(),
        "chooser_kind": kind_filter,
        "focused_source": focused_source,
    }
    operation = (
        request.POST.get("operation")
        if request.method == "POST"
        else request.GET.get("operation")
    )
    if operation not in operations:
        if request.method == "GET" or not operations:
            return render(request, "core/analysis/create.html", chooser_context)
        operation = next(iter(operations))
    if operation == "journey_claims":
        from geo.journey_claim_views import create as create_comparison

        return create_comparison(request, case)
    call_recording = (
        kind_filter in CALL_KINDS and operation in CALL_RECORDING_OPERATIONS
    )
    choose_retained_material = focused_source.count(":") == 1 and (
        call_recording
        or (
            kind_filter in EXTRACTIONS_BY_KIND
            and (operation in TEXT_OPERATIONS or operation in COORDINATE_OPERATIONS)
            and source_readiness(case, operation, [(focused_source, "")])[
                focused_source
            ]["blocking"]
        )
    )
    exact_source = "" if choose_retained_material else focused_source
    fixed_source = (
        evidence_id is not None
        or choose_retained_material
        or (call_recording and evidence is not None)
    )
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
        if not fixed_source
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
        evidence=evidence if fixed_source else None,
        sources_page=sources_page,
        sources_cursor=sources_cursor,
        sources_reverse=sources_reverse,
        selected_sources=(exact_source,) if exact_source else (),
    )
    form.fields["operation"].choices = [
        (key, spec.label) for key, spec in operations.items()
    ]
    if request.method == "GET" and choose_retained_material:
        form.initial.pop("sources", None)
    elif request.method == "GET" and exact_source:
        if exact_source not in dict(form.fields["sources"].choices):
            raise Http404("Source not available for this analysis.")
        form.initial["sources"] = [exact_source]
    if request.method == "POST" and not browse and form.is_valid():
        try:
            selected = form.cleaned_data["sources"]
            if operation == "claim_comparison":
                from text.claim_comparison_views import prepare

                return prepare(
                    request,
                    case,
                    selected,
                    submission_id=form.cleaned_data["submission_id"],
                )
            batch, runs, created = start_selected_analysis(
                request.user,
                case.pk,
                form.cleaned_data["operation"],
                [selection(value) for value in selected],
                form.parameters(),
                submission_id=form.cleaned_data["submission_id"],
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
            if len(runs) == 1:
                return redirect(_url(case, runs[0]))
            return redirect(
                reverse(
                    "core:analysis:processing",
                    args=(case.pk,),
                    query={"batch": batch.pk},
                    fragment="processing-history",
                )
            )
    if browse:
        from django.forms.utils import ErrorDict

        form._errors = ErrorDict()
    return render(
        request,
        "core/analysis/create.html",
        chooser_context
        | {
            "form": form,
            "fixed_source": fixed_source,
            "choose_retained_material": choose_retained_material,
            "choose_call_recording": choose_retained_material and call_recording,
            "has_compatible_source": any(
                not readiness["blocking"] for readiness in form.readiness.values()
            ),
            "has_blocked_selection": form.is_bound
            and any(
                form.readiness.get(value, {}).get("blocking")
                for value in (form["sources"].value() or ())
            ),
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
    finding,
    row,
    source_map,
    case,
    run,
    formatter,
    previews,
    references,
    prior_runs,
    *,
    focused,
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
    item.update(finding_images(finding, row["index"], previews))
    show_excerpt = run.operation in {
        "entities",
        "copied_text",
        "text_topics",
        "content_events",
        "session_summaries",
        "conversation_link",
    } or (focused and run.operation == "question_synthesis")
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
                    "excerpt": _source_excerpt(source, anchors) if show_excerpt else "",
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
                        "Address estimate"
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
    if run.operation == "journey_claims":
        from geo.journey_claim_readers import comparison_sources

        item["comparison_links"] = comparison_sources(item, run, source_map)
    reference_key = values.get("Reference snapshot")
    reference = (
        references.get(reference_key) if isinstance(reference_key, str) else None
    )
    if reference is not None:
        item["reference_snapshot"] = reference
    return item


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


def _add_import_rows(case, item, source_map):
    message_ids = {
        source["id"]: source_map[source["id"]].item.pk
        for source in item["sources"]
        if source_map[source["id"]].kind == "text-message"
    }
    if message_ids:
        rows = dict(
            CommunicationContext.objects.filter(
                case=case,
                source_document__case=case,
                message_id__in=message_ids.values(),
            ).values_list("message_id", "source_row")
        )
        for source in item["sources"]:
            source["import_row"] = rows.get(message_ids.get(source["id"]))


@product_login_required
@require_safe
def detail(request, case_id, run_id):
    case = get_case(request.user, case_id)
    run = _run(case, run_id)
    if run.operation == "sighting_group" and run.status == AnalysisRun.Status.COMPLETE:
        if request.headers.get("X-Pandect-Live") == "1":
            return render_live(
                request, "core/analysis/sighting_ready.html", {"case": case, "run": run}
            )
        return redirect("core:analysis:sighting_detail", case.pk, run.pk)
    history = history_query(request.GET)
    history_back = processing_back_query(history, batch_id=run.batch_id)
    if "batch" not in history_back:
        history.pop("batch", None)
    reader_url = evidence_return_url(case.pk, request.GET)
    if (
        run.operation == "imessage_import"
        and not reader_url
        and request.GET.get("processing") != "1"
    ):
        return redirect(
            reverse("phone:imessage_review", args=[case.pk, run.pk], query=history)
        )
    findings_back = findings_list_query(request.GET)
    back_to_analysis = (
        reverse("core:analysis:processing", args=[case.pk])
        + analysis_query_suffix(history_back)
        + ("#processing-history" if history_back else "")
    )
    back_label = (
        "Back to batch checks" if "batch" in history_back else "Back to Processing"
    )
    if findings_back is not None:
        back_label = "Back to findings"
        back_to_analysis = reverse(
            "core:analysis:browse", args=[case.pk], query=findings_back
        )
        history["return_findings"] = urlencode(findings_back)
    section = "analysis" if findings_back is not None else "processing"
    if reader_url:
        back_label = "Back to evidence"
        back_to_analysis = reader_url
        history["return_evidence"] = request.GET["return_evidence"]
        section = "evidence"
    context = _context(request, case) | {
        "nav_section": section,
        "run": run,
        "history_suffix": analysis_query_suffix(history),
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
    if not context.get("source_unavailable"):
        result = AnalysisResult.objects.filter(
            case=case, run=run, run__case=case
        ).first()
        if result is not None:
            indexed_findings = list(enumerate(result.data.get("items", [])))
            weak_plate_indices = {
                index
                for index, finding in indexed_findings
                if run.operation == "plate_ocr"
                and finding.get("values", {}).get("Weak plate region") is True
            }
            timing_only_indices = {
                index
                for index, finding in indexed_findings
                if is_timing_only(run.operation, finding)
            }
            weak_indices = weak_plate_indices | timing_only_indices
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
            projected = result.findings.filter(case=case)
            if weak_indices and not show_weak:
                projected = projected.filter(weak=False)
            matches, estimates = review_context(projected, case)
            ranked = rank_findings(projected, case, context=(matches, estimates))
            priorities = {
                index: (position, reason, pk)
                for position, (index, reason, pk) in enumerate(
                    ranked.values_list("item_index", "review_reason", "pk")
                )
            }
            # Keep canonical items without a projection inspectable in saved order.
            visible_findings.sort(
                key=lambda entry: priorities.get(entry[0], (len(priorities), ""))[0]
            )
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
            if (
                focused
                and entries
                and run.operation in {"sessions", "session_summaries"}
            ):
                from phone.conversation_instances import result_instance_url

                context["instance_url"] = result_instance_url(
                    case, run, entries[0][0], result.data.get("items", []), sources
                )
            if focused and entries and (priority := priorities.get(entries[0][0])):
                _, reason, pk = priority
                if reason and pk in matches:
                    match_kind, digest = matches[pk]
                    context["review_followup"] = (
                        "Compare matching sources",
                        reverse(
                            "core:analysis:connections",
                            args=[case.pk],
                            query={"identifier": match_kind, "key": digest},
                        ),
                    )
                elif reason and pk in estimates:
                    context["review_followup"] = (
                        "Open analysis that used this estimate",
                        reverse("core:analysis:detail", args=[case.pk, estimates[pk]]),
                    )
            source_map = {str(source.id): source for source in sources}
            entries = [
                (
                    index,
                    {"label": "Source unavailable"}
                    if unavailable_time_claim(run.operation, finding, source_map)
                    else finding,
                )
                for index, finding in entries
            ]
            formatter = measurement_formatter(
                [finding.get("values", {}) for _, finding in entries],
                case=case,
                sources=sources,
                query=request.GET,
            )
            artifacts = result.artifacts.filter(case=case)
            previews = (
                previews_by_result(case, artifacts).get(result.pk, {})
                if run.operation in {"ocr", "plate_ocr"} or focused
                else {}
            )
            if run.operation == "ocr":
                context["layout"] = visible_structure(
                    result,
                    source_map,
                    previews,
                    request.GET,
                    history,
                    entries[0][1] if selected_position is not None else None,
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
                        case,
                        run,
                        show_weak=show_weak,
                        history=history,
                        sources=sources,
                        review_reason=priorities.get(index, (0, ""))[1],
                    ),
                    source_map,
                    case,
                    run,
                    formatter,
                    previews,
                    references,
                    prior_runs,
                    focused=focused,
                )
                for index, finding in entries
            ]
            if run.operation == "geocode_locations":
                decisions = (
                    result.reviews.filter(
                        case=case, item_index__in=[item["index"] for item in items]
                    )
                    .order_by("item_index", "-created_at", "id")
                    .distinct("item_index")
                    .values_list("item_index", "decision")
                )
                in_use = {
                    index
                    for index, decision in decisions
                    if decision == AnalysisReview.Decision.ACCEPTED
                }
                for item in items:
                    item["can_select_estimate"] = address_estimate(item) is not None
                    item["estimate_in_use"] = item["index"] in in_use
            groups, template = result_view(
                run.operation, items, formatter, query=request.GET
            )
            if focused:
                _add_import_rows(case, items[0], source_map)
                context.update(
                    focused_finding_context(run.operation, items[0], result.data)
                )
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
                "metadata_result": run.operation
                in {"image_metadata", "audio_metadata"},
                "finding_page": finding_page,
                "show_weak_regions": show_weak and bool(weak_plate_indices),
                "show_timing_only": show_weak and bool(timing_only_indices),
                "timing_only_count": len(timing_only_indices),
                "weak_region_count": len(
                    {
                        str(finding.get("values", {}).get("Plate region number", index))
                        for index, finding in indexed_findings
                        if index in weak_plate_indices
                    }
                ),
                "reference_snapshots": snapshots,
            }
    context["can_retry"] = run.operation in OPERATIONS and run.status in {
        "failed",
        "cancelled",
        "partial",
        "unsupported",
        "insufficient",
        "empty",
        "complete",
    }
    if context["can_edit"] and context["can_retry"]:
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
        _url(run.case, run)
        + analysis_query_suffix(result_navigation(request.GET, case_id))
    )


@product_login_required
@require_POST
@transaction.atomic
def retry(request, case_id, run_id):
    case = get_case(request.user, case_id, edit=True, lock=True)
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
            _url(case, prior)
            + analysis_query_suffix(result_navigation(request.GET, case_id))
        )
    try:
        submission_id = (
            UUID(request.POST["submission_id"])
            if request.POST.get("submission_id")
            else uuid4()
        )
        existing = AnalysisRun.objects.filter(
            submission_id=submission_id,
            case=case,
            requested_by=request.user,
            previous_run=prior,
        ).first()
        if existing is not None:
            return redirect(
                _url(case, existing)
                + analysis_query_suffix(result_navigation(request.GET, case_id))
            )
        run, _ = retry_run(
            request.user,
            case,
            prior,
            submission_id=submission_id,
        )
    except (ValueError, ValidationError, AnalysisError) as error:
        messages.error(
            request,
            "The service for this run is unavailable."
            if isinstance(error, AnalysisError)
            and error.code == "original_route_unavailable"
            else (
                "The inputs changed or are no longer available. Start a new analysis "
                "and review its source selection."
            ),
        )
        return redirect(
            _url(case, prior)
            + analysis_query_suffix(result_navigation(request.GET, case_id))
        )
    messages.success(request, "A new run is queued; the earlier history is retained.")
    return redirect(
        _url(case, run) + analysis_query_suffix(result_navigation(request.GET, case_id))
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


@product_login_required
@require_safe
def download(request, case_id, run_id):
    case = get_case(request.user, case_id)
    run = _run(case, run_id)
    try:
        sources = resolve_inputs(run, case, require_current_review=False)
        result = AnalysisResult.objects.get(case=case, run=run)
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
    return playback_response(
        request, source.item, kind=source.kind, attachment=source.attachment
    )


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

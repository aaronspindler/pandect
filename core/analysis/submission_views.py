"""Choose exact retained sources and request analysis."""

from urllib.parse import parse_qsl, urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.findings import additional_operations
from core.analysis.form_base import (
    CALL_RECORDING_OPERATIONS,
    EXTRACTIONS_BY_KIND,
)
from core.analysis.forms import analysis_form
from core.analysis.manual import start_selected_analysis
from core.analysis.navigation import query_suffix, run_url
from core.analysis.operations import (
    CASE_OPERATIONS,
    INPUT_GUIDANCE,
    OPERATION_GROUPS,
    OPERATIONS,
)
from core.analysis.readiness import (
    COORDINATE_OPERATIONS,
    TEXT_OPERATIONS,
    source_readiness,
)
from core.analysis.sources import selection
from core.evidence.navigation import collection_params
from core.evidence.queries import get_evidence
from core.evidence.types import CALL_KINDS, KIND_LABELS


def operation_groups(operations, params, *, base_url=""):
    return [
        (
            group,
            [
                {
                    "key": key,
                    "label": operations[key].label,
                    "description": description,
                    "url": base_url + query_suffix({"operation": key} | params),
                }
                for key, description in choices.items()
                if key in operations
            ],
        )
        for group, choices in OPERATION_GROUPS.items()
        if operations.keys() & choices.keys()
    ]


@product_login_required
@require_http_methods(["GET", "POST"])
def create(request, case_id, kind=None, evidence_id=None):
    case = get_case(request.user, case_id, edit=True)
    evidence = None
    evidence_url = ""
    reader_params = {}
    collection_origin = False
    focused_source = request.GET.get("source", "")
    all_sources = (
        evidence_id is None
        and bool(focused_source)
        and request.GET.get("all_sources") == "1"
    )
    operations = CASE_OPERATIONS
    if evidence_id is not None:
        item = get_evidence(case, kind, evidence_id)
        evidence = (kind, item)
        operations = additional_operations(kind, item)
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
    if evidence and evidence[1].is_archived:
        raise Http404("Restore this evidence before analyzing it.")
    encoded = request.GET.get("return_collection")
    if encoded is not None and len(encoded) <= 3000:
        try:
            reader_params = collection_params(
                dict(parse_qsl(encoded, max_num_fields=7))
            )
        except ValueError:
            pass
        else:
            collection_origin = True
    if evidence:
        evidence_url = reverse(
            "core:evidence:detail",
            args=[case.pk, evidence[0], evidence[1].pk],
            query=reader_params,
        )
    elif collection_origin:
        evidence_url = reverse(
            "core:evidence:list", args=[case.pk], query=reader_params
        )
    kind_filter = evidence[0] if evidence else request.GET.get("type", "")
    if kind_filter not in KIND_LABELS:
        kind_filter = ""
    if kind_filter:
        operations = {
            name: spec for name, spec in operations.items() if kind_filter in spec.kinds
        }
    chooser_params = {"source": focused_source} if focused_source else {}
    if all_sources:
        chooser_params["all_sources"] = "1"
    if kind_filter and not evidence:
        chooser_params["type"] = kind_filter
    if collection_origin:
        chooser_params["return_collection"] = urlencode(reader_params)
    result_query = {}
    if evidence:
        result_query["return_evidence"] = urlencode(
            {"kind": evidence[0], "evidence": evidence[1].pk, "reader": "1"}
            | reader_params
        )
    elif collection_origin:
        result_query["return_evidence"] = urlencode({"reader": "list"} | reader_params)
    chooser_context = case_context(case, request.user) | {
        "nav_section": "analysis",
        "operation_groups": operation_groups(operations, chooser_params),
        "evidence": evidence[1] if evidence else None,
        "evidence_url": evidence_url,
        "collection_query": chooser_params.get("return_collection"),
        "evidence_collection_suffix": query_suffix(reader_params),
        "chooser_url": request.path + query_suffix(chooser_params),
        "chooser_kinds": KIND_LABELS.items(),
        "chooser_kind": kind_filter,
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

        return create_comparison(
            request, case, chooser_query=chooser_params, result_query=result_query
        )
    call_recording = (
        kind_filter in CALL_KINDS and operation in CALL_RECORDING_OPERATIONS
    )
    choose_retained_material = (
        not all_sources
        and focused_source.count(":") == 1
        and (
            call_recording
            or (
                kind_filter in EXTRACTIONS_BY_KIND
                and (operation in TEXT_OPERATIONS or operation in COORDINATE_OPERATIONS)
                and source_readiness(case, operation, [(focused_source, "")])[
                    focused_source
                ]["blocking"]
            )
        )
    )
    exact_source = "" if choose_retained_material or all_sources else focused_source
    fixed_source = (
        evidence_id is not None
        or choose_retained_material
        or (call_recording and evidence is not None and not all_sources)
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
    form_options = {
        "actor": request.user,
        "case": case,
        "operation": operation,
        "query": query,
        "evidence": evidence if fixed_source else None,
        "sources_page": sources_page,
        "sources_cursor": sources_cursor,
        "sources_reverse": sources_reverse,
        "selected_sources": (exact_source,) if exact_source else (),
    }
    try:
        form = analysis_form(posted, **form_options)
    except Http404:
        if operation != "claim_comparison" or not sources_cursor:
            raise
        form_options.update(sources_page=1, sources_cursor="", sources_reverse=False)
        form = analysis_form(posted, **form_options)
        messages.info(request, "The source list changed. Showing the first page.")
    if operation == "claim_comparison" and browse == "review":
        choices = dict(form.fields["sources"].choices)
        if any(value not in choices for value in posted.getlist("sources")):
            messages.info(
                request, "Unavailable sources were cleared from the selection."
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
                    chooser_query=chooser_params,
                    result_query=result_query,
                    picker_state={
                        "q": query,
                        "sources_page": form.source_page.number,
                        "sources_cursor": form.sources_cursor,
                        "sources_reverse": "1" if form.sources_reverse else "",
                    },
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
                return redirect(
                    run_url(runs[0].case_id, runs[0].pk) + query_suffix(result_query)
                )
            return redirect(
                reverse(
                    "core:analysis:processing",
                    args=(case.pk,),
                    query={"batch": batch.pk} | result_query,
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
            "case_sources_url": reverse(
                "core:analysis:create",
                args=[case.pk],
                query=chooser_params | {"operation": operation, "all_sources": "1"},
            )
            if choose_retained_material
            else "",
            "choose_call_recording": choose_retained_material and call_recording,
            "has_compatible_source": any(
                not readiness["blocking"] for readiness in form.readiness.values()
            ),
            "has_blocked_selection": form.is_bound
            and any(
                form.readiness.get(value, {}).get("blocking")
                for value in (form["sources"].value() or ())
            ),
            "search_notice": form.search_notice,
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

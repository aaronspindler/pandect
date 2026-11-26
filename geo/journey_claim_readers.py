"""Read an exact passage or saved group used by a published comparison."""

from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError
from core.analysis.navigation import finding_url, result_navigation
from core.analysis.source_resolution import resolve_active_inputs
from core.evidence.types import COMMUNICATION_KINDS
from core.models import AnalysisResult
from geo.trip_processing import saved_trips


def comparison_sources(item, run, sources, *, history=None):
    """Link each cited passage, observation and group."""
    links = []
    args = (run.case_id, run.pk, item["index"])
    for ordinal, anchor in enumerate(item["anchors"]):
        source = sources[anchor["source_id"]]
        if "start" in anchor:
            links.append(
                (
                    f"Read passage from {source.item.title} · "
                    f"characters {anchor['start']}–{anchor['end']}",
                    reverse("geo:plan_passage", args=(*args, ordinal), query=history),
                )
            )
        else:
            links.append((source.item.title, source.url))
            if source.derived_result_id:
                links.append(("Open original observation", source.original_url))
            if getattr(source.item, "source_row", None):
                links.append(
                    (
                        f"Open source CSV row {source.item.source_row}",
                        source.original_url + "?info=1#original-csv-row",
                    )
                )
    claim = run.parameters["claims"][item["claim_index"]]
    if claim.get("result_id"):
        links.append(
            (
                "Open source claim · AI suggestion",
                finding_url(run.case_id, claim["run_id"], claim["item_index"]),
            )
        )
    for index in item.get("group_indices", []):
        group = run.parameters["groups"][index]
        links.append(
            (
                f"Inspect {group['label']}",
                reverse(
                    "geo:plan_group",
                    args=(run.case_id, run.pk, index),
                    query=(history or {}) | {"finding": item["index"] + 1},
                ),
            )
        )
    return links


def _published_comparison(request, case_id, run_id):
    case = get_case(request.user, case_id)
    result = get_object_or_404(
        AnalysisResult.objects.select_related("run"),
        case=case,
        run__case=case,
        run_id=run_id,
        run__operation="journey_claims",
    )
    try:
        sources = resolve_active_inputs(result.run, case, require_current_review=False)
    except AnalysisError as error:
        raise Http404("Comparison sources are unavailable.") from error
    return case, result, {source.id: source for source in sources}


@product_login_required
@require_safe
def passage(request, case_id, run_id, item_index, ordinal):
    case, result, sources = _published_comparison(request, case_id, run_id)
    try:
        item = result.data["items"][item_index]
        anchor = item["anchors"][ordinal]
        source = sources[anchor["source_id"]]
        start, end = anchor["start"], anchor["end"]
        body = source.text
        if not (
            source.id in item["source_ids"]
            and type(start) is int
            and type(end) is int
            and 0 <= start < end <= len(body)
        ):
            raise ValueError
    except (IndexError, KeyError, TypeError, ValueError) as error:
        raise Http404("Passage not found.") from error
    # Derive native links from the verified source instead of trusting stored URLs.
    parts = source.anchor(start=start, end=end).get("extraction_ranges", [])
    for part in parts:
        part["url"] = finding_url(case.pk, source.derived_run_id, part["finding_index"])
    history = result_navigation(request.GET, case.pk)
    return render(
        request,
        "geo/analysis/plan_passage.html",
        case_context(case, request.user)
        | {
            "nav_section": "evidence" if "return_evidence" in history else "analysis",
            "source": source,
            "parts": parts,
            "start": start,
            "end": end,
            "before": body[max(0, start - 200) : start],
            "quote": body[start:end],
            "after": body[end : end + 200],
            "run": result.run,
            "back_url": finding_url(case.pk, run_id, item_index, history=history),
            "exchange_url": reverse(
                "core:analysis:browse",
                args=(case.pk,),
                query={
                    "kind": source.kind,
                    "evidence": source.item.pk,
                    "method": "conversation_link",
                    "weak": "1",
                },
            )
            if source.kind in COMMUNICATION_KINDS
            else "",
        },
    )


@product_login_required
@require_safe
def group(request, case_id, run_id, index):
    case, result, sources = _published_comparison(request, case_id, run_id)
    params = result.run.parameters
    try:
        selected = params["groups"][index]
        members = [sources[params["observations"][i]] for i in selected["members"]]
    except (KeyError, IndexError, TypeError) as error:
        raise Http404("Group not found.") from error
    current_url = ""
    if selected["origin"] == "trip":
        if (
            saved_trips(case)
            .filter(pk=selected["trip_id"], generation_id=selected["generation_id"])
            .exists()
        ):
            current_url = reverse(
                "geo:trip_detail", args=(case.pk, selected["trip_id"])
            )
    else:
        current_url = finding_url(case.pk, selected["run_id"], selected["item_index"])
    history = result_navigation(request.GET, case.pk)
    back_url = reverse("core:analysis:detail", args=(case.pk, run_id), query=history)
    finding = request.GET.get("finding", "")
    items = result.data.get("items", [])
    if finding.isascii() and finding.isdecimal() and len(finding) <= 9:
        item_index = int(finding) - 1
        if 0 <= item_index < len(items) and index in items[item_index].get(
            "group_indices", ()
        ):
            back_url = finding_url(case.pk, run_id, item_index, history=history)
    return render(
        request,
        "geo/analysis/plan_group.html",
        case_context(case, request.user)
        | {
            "nav_section": "evidence" if "return_evidence" in history else "analysis",
            "group": selected,
            "run": result.run,
            "page": Paginator(members, 25).get_page(request.GET.get("page")),
            "group_url": current_url,
            "back_url": back_url,
        },
    )

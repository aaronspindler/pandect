"""Assemble and page saved findings from authorized versions of their sources."""

from django.conf import settings
from django.core.paginator import Paginator
from django.urls import reverse

from core.analysis.address_estimates import address_estimate
from core.analysis.findings import finding_row
from core.analysis.important import has_available_citations, retained_finding_url
from core.analysis.navigation import (
    FINDINGS_PER_PAGE,
    finding_url,
    query_suffix,
)
from core.analysis.presentation import measurement_formatter
from core.analysis.projection import is_timing_only
from core.analysis.ranking import rank_findings, review_context
from core.analysis.result_views import focused_finding_context, result_view
from core.evidence.time_claims import unavailable_time_claim
from core.evidence.types import KIND_LABELS
from core.important.reader import reader_context
from core.models import AnalysisArtifact, AnalysisResult, AnalysisReview
from geo.analysis import presentation as geo_presentation
from image.analysis.presentation import (
    finding_images,
    previews_by_result,
    visible_structure,
)
from phone.analysis import presentation as phone_presentation
from phone.models import CommunicationContext


def retained_result_context(request, case, run, sources, *, history, finding_history):
    result = AnalysisResult.objects.filter(case=case, run=run, run__case=case).first()
    if result is None:
        return {}
    context = {}
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
    if focused and entries and run.operation in {"sessions", "session_summaries"}:
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
    if (
        focused
        and run.operation != "sighting_group"
        and has_available_citations(run.operation, entries[0][1], source_map)
    ):
        index = entries[0][0]
        source_url = retained_finding_url(
            case.pk,
            run,
            index,
            show_weak=show_weak,
            history=history,
        )
        context.update(
            reader_context(
                request,
                case,
                finding_result=result,
                item_index=index,
                source_url=source_url,
            )
        )
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
            finding_history,
            entries[0][1] if selected_position is not None else None,
        )
    snapshots = list(artifacts.filter(kind=AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE))
    references = {
        snapshot.metadata.get("reference_key"): snapshot for snapshot in snapshots
    }
    prior_runs = {
        str(pk): run_id
        for pk, run_id in AnalysisResult.objects.filter(
            case=case,
            run__case=case,
            pk__in=[ref["result_id"] for ref in run.parameters.get("finding_refs", [])],
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
                history=finding_history,
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
            history=history,
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
    groups, template = result_view(run.operation, items, formatter, query=request.GET)
    if focused:
        _add_import_rows(case, items[0], source_map)
        context.update(focused_finding_context(run.operation, items[0], result.data))
    run_query = finding_history | ({"weak": 1} if show_weak else {})
    map_points = (
        geo_presentation.map_points(run.operation, items)
        if run.operation in geo_presentation.PRIMARY_FIELDS
        else []
    )
    return context | {
        "result": result,
        "result_groups": groups,
        "result_template": template,
        "run_query": run_query,
        "run_suffix": query_suffix(run_query),
        "map_points": map_points,
        "position_plot": len({(p["latitude"], p["longitude"]) for p in map_points}) > 1,
        "position_map_available": bool(settings.PANDECT_MAP_TILE_URL)
        and any(point["plottable"] for point in map_points),
        "data": result.data,
        "metrics": measurement_formatter(
            result.data.get("metrics", {}),
            case=case,
            sources=sources,
            query=request.GET,
        )(result.data.get("metrics", {})),
        "items": items,
        "focused_finding": focused,
        "metadata_result": run.operation in {"image_metadata", "audio_metadata"},
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
    history,
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
            original_url = source.original_url if source.derived_result_id else url
            if source.kind == "document":
                pages = {
                    anchor["page"] for anchor in display_anchors if anchor.get("page")
                }
                if len(pages) == 1:
                    original_url += f"?document_page={pages.pop()}#document-preview"
                    if source.derived_result_id is None:
                        url = original_url
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
                    "original_url": original_url if source.derived_result_id else None,
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

        item["comparison_links"] = comparison_sources(
            item, run, source_map, history=history
        )
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

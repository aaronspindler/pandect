"""Validate and present exact retained findings for case Important items."""

from django.core.exceptions import ValidationError
from django.http import Http404
from django.urls import reverse

from core.analysis.address_estimates import require_current_geocode_reviews
from core.analysis.errors import AnalysisError
from core.analysis.findings import finding_row
from core.analysis.navigation import finding_url
from core.analysis.projection import is_timing_only
from core.analysis.sightings import (
    review_outcome,
    saved_group,
    sighting_reviews,
    validate_current_sighting,
)
from core.analysis.source_resolution import SourceResolutionCache, resolve_inputs
from core.evidence.time_claims import unavailable_time_claim


def has_available_citations(operation, finding, source_map):
    """Check canonical citations even when there is no searchable projection."""
    if (
        not isinstance(finding, dict)
        or not isinstance(finding.get("label"), str)
        or not isinstance(finding.get("text", ""), str)
        or not isinstance(finding.get("values", {}), dict)
    ):
        return False
    cited = finding.get("source_ids")
    return (
        isinstance(cited, list)
        and bool(cited)
        and all(isinstance(key, str) and key in source_map for key in cited)
        and not unavailable_time_claim(operation, finding, source_map)
    )


def retained_finding_url(case_id, run, item_index, *, history=None, show_weak=False):
    if run.operation == "sighting_group":
        return reverse(
            "core:analysis:sighting_detail", args=[case_id, run.pk], query=history
        )
    if run.operation == "imessage_import":
        history = (history or {}) | {"processing": "1"}
    return finding_url(
        case_id, run.pk, item_index, history=history, show_weak=show_weak
    )


def finding_target(case, result, item_index, *, finding=None, cache=None):
    """Return a row only when the requested canonical finding remains readable."""
    run = result.run
    if (
        result.case_id != case.pk
        or run.case_id != case.pk
        or (run.previous_run_id and run.previous_run.case_id != case.pk)
        or item_index < 0
        or (run.operation == "sighting_group" and item_index != 0)
    ):
        raise Http404("Finding unavailable.")
    try:
        cache = cache or SourceResolutionCache(case, active_only=True)
        if finding is None:
            finding = result.data["items"][item_index]
        if run.operation == "sighting_group":
            _, sighting, sources = saved_group(case, run, cache=cache)
        else:
            sources = resolve_inputs(
                run, case, require_current_review=False, cache=cache
            )
    except (
        AnalysisError,
        Http404,
        ValidationError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise Http404("Finding unavailable.") from None

    source_map = {str(source.id): source for source in sources}
    if not has_available_citations(run.operation, finding, source_map):
        raise Http404("Finding unavailable.")
    row = finding_row(finding, item_index, case, run, sources=sources)
    title = row["short_value"] or row["label"]
    label = row["label"] if row["short_value"] else "Finding"
    qualifiers = []
    if row["ai_suggestion"]:
        qualifiers.append("AI suggestion")
    if run.status == "partial":
        qualifiers.append("Partial coverage")
    if is_timing_only(run.operation, finding):
        qualifiers.append("Timing only")
    if row["weak_plate_region"]:
        qualifiers.append("Weak plate region")
    if row["low_ocr_score"]:
        qualifiers.append("Low OCR score")
    if run.operation == "sighting_group":
        title = f"Sighting group · {sighting['anchor']['title']}"
        label = "Saved proposal"
        try:
            validate_current_sighting(run, case, sources)
        except AnalysisError:
            label = "Historical proposal"
        latest = sighting_reviews(case, result).only("decision").first()
        qualifiers.append(review_outcome(latest))
    else:
        try:
            require_current_geocode_reviews(run, sources)
        except AnalysisError as error:
            if error.code != "geocode_review_required":
                raise Http404("Finding unavailable.") from None
            qualifiers.append("Address estimate no longer selected")
    return {
        "title": title,
        "label": label,
        "qualifiers": qualifiers,
        "url": retained_finding_url(case.pk, run, item_index),
    }

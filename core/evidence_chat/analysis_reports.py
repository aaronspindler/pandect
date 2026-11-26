"""Search saved method outcomes and measurements, including zero-finding results."""

from django.db.models import F, Q
from django.db.models.expressions import RawSQL
from django.urls import reverse

from core.analysis.operations import OPERATIONS
from core.analysis.published import successful_results

from . import corpus
from .reports import read_timing_material

REPORT_TERMS = {
    "analysis",
    "analyses",
    "check",
    "checks",
    "measurement",
    "measurements",
    "method",
    "methods",
    "report",
    "reports",
    "result",
    "results",
    "saved",
}


def eligible_results(case):
    return (
        successful_results(case)
        .filter(run__operation__in=OPERATIONS)
        .exclude(run__parameters__contains={"_search_generation": "briefs"})
    )


def overlapping_results(results, case, locators):
    """Use the immutable input manifest, including unquoted members of large reports."""
    original_ids = {
        (loc["kind"], loc["pk"]) for loc in locators if loc["surface"] == "original"
    }
    report_ids = [loc["pk"] for loc in locators if loc["surface"] == "report"]
    origin_results = {
        loc["result_id"]
        for loc in locators
        if loc["surface"] in ("finding", "passage") and loc.get("result_id")
    }
    match = Q(pk__in=origin_results)
    for kind, key in original_ids:
        match |= Q(run__inputs__contains=[{"kind": kind, "evidence_id": str(key)}])
    if report_ids:
        # The run-input GIN index supports containment without expanding every
        # candidate run in Python. Both the prior and later selections are case scoped.
        match |= Q(
            run_id__in=RawSQL(
                """
            SELECT later.id
              FROM core_analysisresult old
              JOIN core_analysisrun r ON r.id=old.run_id AND r.case_id=old.case_id
              CROSS JOIN LATERAL jsonb_array_elements(r.inputs) AS source
              JOIN core_analysisrun later ON later.case_id=r.case_id AND
                   later.inputs @> jsonb_build_array(jsonb_build_object(
                       'kind',source->>'kind','evidence_id',source->>'evidence_id'))
             WHERE old.id=ANY(%s::uuid[]) AND old.case_id=%s
            """,
                [report_ids, case.pk],
            )
        )
    return results.filter(match)


def read_analysis_material(case, result_id, start, size, *, through=None):
    results = eligible_results(case).filter(pk=result_id)
    if through:
        results = results.filter(created_at__lte=through)
    result = (
        results.select_related("run")
        .defer("data", "provenance", "run__inputs", "run__parameters")
        .annotate(measurements=F("report_index__text"))
        .first()
    )
    if result is None:
        return None
    run = result.run
    if run.operation == "comm_patterns":
        return read_timing_material(case, result_id, start, size, through=through)
    if result.measurements is None:
        return None
    sources = corpus.result_sources(case, run, include_archived=False)
    if not sources:
        return None
    members = sorted({(s.kind, str(s.item.pk)) for s in sources})
    method = OPERATIONS[run.operation].label
    _, _, measures = result.measurements.partition("\n")
    heading = corpus.lines(
        [
            ("Method", method),
            ("Method version", run.method_version),
            ("Outcome", run.get_status_display()),
            ("Selected inputs", len(sources)),
            (
                "Coverage",
                "Selected inputs only. Open Processing for the full saved result.",
            ),
        ]
    )
    material = corpus.make_material(
        "report",
        run.operation,
        None,
        result,
        [corpus.dependency(kind, key) for kind, key in members[:64]],
        f"Saved analysis · {method}",
        reverse(
            "core:analysis:detail",
            args=(case.pk, run.pk),
            query={"summary": "1"} if measures else None,
            fragment="processing-summary" if measures else None,
        ),
        version=[str(result.pk), run.equivalence_key, run.status],
        suffix=heading + ("\n" + measures if measures else ""),
        gap="The saved analysis has partial coverage."
        if run.status == "partial"
        else "",
        start=start,
        size=size,
    )
    material.locator["result_id"] = str(result.pk)
    material.locator["selected_count"] = len(sources)
    return material

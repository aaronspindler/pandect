"""Search saved method outcomes and measurements, including zero-finding results."""

from functools import reduce
from operator import or_

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.expressions import RawSQL
from django.urls import reverse

from core.analysis.catalogue import SUCCESSFUL
from core.analysis.operations import OPERATIONS
from core.analysis.report_index import report_words
from core.models import AnalysisReportIndex, AnalysisResult

from . import corpus
from .corpus_search import terms_for
from .reports import MAX_REPORTS, read_timing_material

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
        AnalysisResult.objects.filter(
            case=case,
            run__case=case,
            run__status__in=SUCCESSFUL,
            run__operation__in=OPERATIONS,
        )
        .filter(Q(run__previous_run__isnull=True) | Q(run__previous_run__case=case))
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


def analysis_materials(
    case, query, *, through, sources=None, after=None, automatic=False, methods=()
):
    terms = terms_for(query, extra_stopwords=REPORT_TERMS | {"recent", "latest"})
    if automatic and not terms and not set(query.casefold().split()) & REPORT_TERMS:
        return []
    results = eligible_results(case).filter(created_at__lte=through)
    newer = results.filter(
        run__equivalence_key=OuterRef("run__equivalence_key")
    ).filter(
        Q(run__created_at__gt=OuterRef("run__created_at"))
        | Q(run__created_at=OuterRef("run__created_at"), run_id__gt=OuterRef("run_id"))
    )
    results = results.filter(~Exists(newer))
    if after:
        results = results.filter(created_at__gt=after)
    if sources is not None:
        results = overlapping_results(results, case, sources)
    if methods:
        results = results.filter(run__operation__in=methods)
    if automatic:
        # Existing timing requests use their own semantics, including coverage,
        # correct units and source population. Other runs stay discoverable.
        results = results.exclude(run__operation="comm_patterns")
    # Query the index expression on its owning table. Only the bounded contents
    # and fields of the selected results are materialized.
    index = AnalysisReportIndex.objects.filter(result__in=results)
    order = ["-result__run__created_at", "-result__run_id"]
    if terms:
        # Search the same GIN expression for both named measurements and
        # operation labels. An OR against another table would force PostgreSQL
        # to examine the unrelated reports of a busy case before ranking.
        words = set(terms)
        method_names = [
            key.replace("_", " ")
            for key, op in OPERATIONS.items()
            if words.intersection(terms_for(op.label))
        ]
        query = reduce(
            or_,
            (SearchQuery(term, config="english") for term in (*terms, *method_names)),
        )
        index = (
            index.alias(words=report_words())
            .filter(words=query)
            .annotate(score=SearchRank(report_words(), query))
        )
        order.insert(0, "-score")
    output = []
    for key in index.order_by(*order).values_list("result_id", flat=True)[
        : MAX_REPORTS * 2
    ]:
        if material := read_analysis_material(
            case, key, 0, corpus.MAX_WINDOW, through=through
        ):
            output.append(material)
            if automatic or len(output) >= MAX_REPORTS:
                break
    return output


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


def answer_window(material, query, limit):
    """Quote the matching named measurement even in a long processing summary."""
    from .engine import Window

    start = 0
    if len(material.text) > limit:
        terms = set(terms_for(query, extra_stopwords=REPORT_TERMS))
        best_score = offset = 0
        for line in material.text.splitlines(keepends=True):
            score = len(terms.intersection(terms_for(line.partition(":")[0])))
            if score > best_score:
                best_score = score
                start = max(0, offset + len(line) - limit)
            offset += len(line)
    return Window(material.locator, start, material.text[start : start + limit])

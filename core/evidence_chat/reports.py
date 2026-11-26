"""Read and revalidate bounded, saved message-timing report values for chat."""

from datetime import timedelta

from django.db.models import F, OuterRef, Q, Subquery
from django.urls import reverse

from core.analysis.catalogue import SUCCESSFUL
from core.models import AnalysisResult

from . import corpus

# Store all original identities for attribution and resolve at most 192 selected
# messages in one lookup. Reports with larger selections remain in Processing.
MAX_INPUTS = 64
MAX_REPORTS = 3
METRICS = (
    "records",
    "timed_records",
    "first_recorded_at",
    "last_recorded_at",
    "response_interval_count",
    "median_response_interval_seconds",
)


def _results(case):
    return AnalysisResult.objects.filter(
        case=case,
        run__case=case,
        run__operation="comm_patterns",
        run__status__in=SUCCESSFUL,
    ).filter(Q(run__previous_run__isnull=True) | Q(run__previous_run__case=case))


def _metrics(results):
    # PostgreSQL projects just these scalars. The rest of data can contain every
    # original message as well as long lists of individual measurements.
    return (
        results.filter(
            run__inputs__0__isnull=False, **{f"run__inputs__{MAX_INPUTS}__isnull": True}
        )
        .select_related("run")
        .only(
            "pk",
            "created_at",
            "run__case",
            "run__operation",
            "run__method_version",
            "run__status",
            "run__inputs",
        )
        .annotate(**{name: F(f"data__metrics__{name}") for name in METRICS})
    )


def _material(case, result, *, start=0, size=corpus.MAX_WINDOW):
    run = result.run
    # This method operates on original direct messages only. Other snapshot
    # types would need their own source/coverage contract.
    if not all(
        isinstance(s, dict)
        and s.get("kind") == "text-message"
        and not s.get("result_id")
        and not s.get("attachment_id")
        for s in run.inputs
    ):
        return None
    sources = corpus.result_sources(case, run, include_archived=False)
    if not sources:
        return None
    records, timed, intervals = (
        result.records,
        result.timed_records,
        result.response_interval_count,
    )
    median = result.median_response_interval_seconds
    if (
        any(type(n) is not int for n in (records, timed, intervals))
        or records != len(sources)
        or not 0 <= timed <= records
        or not 0 <= intervals <= max(0, timed - 1)
        or (intervals == 0) != (median is None)
    ):
        return None
    if median is not None and (
        type(median) not in (int, float)
        or not 0 <= median <= timedelta.max.total_seconds()
    ):
        return None
    times = (result.first_recorded_at, result.last_recorded_at)
    if timed and not all(isinstance(t, str) and 0 < len(t) <= 40 for t in times):
        return None
    if not timed and any(t is not None for t in times):
        return None
    # A short list identifies the population without including message content.
    people = {
        person.pk: str(person)
        for source in sources
        for person in (source.item.sender, source.item.recipient)
    }
    participants = ", ".join(
        name if len(name) <= 55 else name[:54] + "…"
        for name in sorted(people.values())[:3]
    )
    if len(people) > 3:
        participants += f" and {len(people) - 3} more"
    text = corpus.lines(
        (
            ("Selected messages", records),
            ("Participants in selection", participants),
            ("Messages with recorded times", timed),
            ("First recorded time", times[0]),
            ("Last recorded time", times[1]),
            ("Response interval count", intervals),
            (
                "Median response interval (seconds)",
                median if median is not None else "Unavailable",
            ),
            (
                "Measure",
                "Intervals between consecutive different-sender selected messages in "
                "inferred sessions. Actual response or reading times are unknown.",
            ),
        )
    )
    label = f"Saved message timing · {result.created_at.isoformat()}"
    dependencies = [
        corpus.dependency("text-message", key)
        for key in sorted({str(s.item.pk) for s in sources})
    ]
    found = corpus.make_material(
        "report",
        "comm_patterns",
        None,
        result,
        dependencies,
        label,
        reverse(
            "core:analysis:detail",
            args=(case.pk, run.pk),
            query={"summary": "1"},
            fragment="processing-summary",
        ),
        version=[str(result.pk), str(run.pk), run.method_version, run.status],
        suffix=text,
        gap="The saved report has partial coverage." if run.status == "partial" else "",
        start=start,
        size=size,
    )
    found.locator["result_id"] = str(result.pk)
    return found


def timing_materials(case, *, through, sources=None, after=None):
    """Read up to three recent, current selections visible at this turn's cutoff."""
    results = _results(case).filter(created_at__lte=through)
    latest = results.filter(
        run__equivalence_key=OuterRef("run__equivalence_key")
    ).order_by("-run__created_at", "-run_id")
    matches = results.filter(pk=Subquery(latest.values("pk")[:1]))
    if after:
        matches = matches.filter(created_at__gt=after)
    if sources is not None:
        message_ids = {
            source["pk"]
            for source in sources
            if source.get("surface") == "original"
            and source.get("kind") == "text-message"
        }
        for source in sources:
            if source.get("surface") == "report" and source["kind"] == "comm_patterns":
                message_ids.update(
                    dep["id"]
                    for dep in source["dependencies"]
                    if dep["kind"] == "text-message"
                )
        if not message_ids:
            return []
        linked = Q()
        for message_id in message_ids:
            linked |= Q(
                run__inputs__contains=[
                    {"kind": "text-message", "evidence_id": message_id}
                ]
            )
        matches = matches.filter(linked)
    candidates = _metrics(matches).order_by("-run__created_at", "-run_id")[:MAX_REPORTS]
    return [
        material for candidate in candidates if (material := _material(case, candidate))
    ]


def read_timing_material(case, result_id, start, size, *, through=None):
    """Reopen the pinned version, including a still-readable superseded result."""
    results = _results(case).filter(pk=result_id)
    if through is not None:
        results = results.filter(created_at__lte=through)
    result = _metrics(results).first()
    return (
        _material(case, result, start=start, size=size) if result is not None else None
    )

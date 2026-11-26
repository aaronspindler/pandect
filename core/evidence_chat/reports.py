"""Read and revalidate bounded, saved message-timing report values for chat."""

from datetime import timedelta

from django.db.models import Case, F, Func, IntegerField, OuterRef, Subquery, When
from django.db.models.functions import Cast, Coalesce
from django.urls import reverse

from core.analysis.published import successful_results
from core.analysis.report_index import TIMING_FIELDS
from phone.analysis.saved_timing import INLINE_MEMBERS, timing_members

from . import corpus

# Existing small reports keep their full original attribution and citation text.
# Large reports pin the entire selection by result and expose a bounded sample.
MAX_REPORTS = 3


def _results(case):
    return successful_results(case).filter(run__operation="comm_patterns")


def _metrics(results):
    # PostgreSQL projects just these scalars. The rest of data can contain every
    # original message as well as long lists of individual measurements.
    return (
        results.select_related("run")
        .only(
            "pk",
            "created_at",
            "run__case",
            "run__operation",
            "run__method_version",
            "run__status",
        )
        .annotate(
            input_count=Coalesce(
                Cast(F("report_index__verified_messages__count"), IntegerField()),
                Func(
                    "run__inputs",
                    function="jsonb_array_length",
                    output_field=IntegerField(),
                ),
            ),
            **{
                name: Case(
                    When(
                        report_index__isnull=False,
                        then=F(f"report_index__timing_values__{name}"),
                    ),
                    default=F(f"data__metrics__{name}"),
                )
                for name in TIMING_FIELDS
            },
        )
        .filter(input_count__gt=0)
    )


def _material(case, result, *, start=0, size=corpus.MAX_WINDOW):
    run = result.run
    # This method operates on original direct messages only. Other snapshot
    # types would need their own source/coverage contract.
    if result.input_count > INLINE_MEMBERS:
        member_keys = timing_members(case, result)
        if not member_keys:
            return None
        participants = "See the full selection in Processing."
    else:
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
        member_keys = sorted({str(s.item.pk) for s in sources})
    records, timed, intervals = (
        result.records,
        result.timed_records,
        result.response_interval_count,
    )
    median = result.median_response_interval_seconds
    if (
        any(type(n) is not int for n in (records, timed, intervals))
        or records != result.input_count
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
    dependencies = [corpus.dependency("text-message", key) for key in member_keys]
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
    if result.input_count > INLINE_MEMBERS:
        found.locator["selected_count"] = result.input_count
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
        from .analysis_reports import overlapping_results

        matches = overlapping_results(
            matches,
            case,
            [
                loc
                for loc in sources
                if loc.get("kind") in ("text-message", "comm_patterns")
            ],
        )
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

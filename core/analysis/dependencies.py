"""Shared validation of pinned findings and their source snapshots."""

from django.core.exceptions import ValidationError

from core.analysis.errors import AnalysisError
from core.models import AnalysisResult


def validate_references(run, sources, case, *, cache=None, depth=0):
    """Read-side dependency checks without loading retained result payloads."""
    from uuid import UUID

    from core.analysis.services import SourceResolutionCache, resolve_inputs

    cache = cache or SourceResolutionCache(case)
    if cache.case.pk != case.pk:
        raise AnalysisError("source_unavailable")
    heights = cache.finding_dependency_heights
    active = cache.finding_dependency_stack
    if depth >= 8 or run.pk in active:
        raise AnalysisError("source_unavailable")
    if run.pk in heights:
        if depth + heights[run.pk] > 8:
            raise AnalysisError("source_unavailable")
        return
    refs = run.parameters.get("finding_refs", [])
    if not isinstance(refs, list) or len(refs) > 500:
        raise AnalysisError("source_unavailable")
    grouped = {}
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or type(ref.get("item_index")) is not int
            or ref["item_index"] < 0
        ):
            raise AnalysisError("source_unavailable")
        try:
            result_id = str(UUID(str(ref.get("result_id"))))
        except ValueError, TypeError, AttributeError:
            raise AnalysisError("source_unavailable") from None
        grouped.setdefault(result_id, set()).add(ref["item_index"])

    inputs = {entry["id"]: entry for entry in run.inputs}
    height = 1
    active.add(run.pk)
    try:
        for result_id, indices in grouped.items():
            if result_id not in cache.finding_results:
                try:
                    result = (
                        AnalysisResult.objects.select_related("run")
                        .defer("data", "provenance")
                        .get(
                            pk=result_id,
                            case=case,
                            run__case=case,
                            run__status__in=("complete", "partial", "empty"),
                        )
                    )
                except AnalysisResult.DoesNotExist, ValueError, ValidationError:
                    raise AnalysisError("source_unavailable") from None
                cache.finding_results[result_id] = (
                    result,
                    set(
                        result.findings.filter(case=case).values_list(
                            "item_index", flat=True
                        )
                    ),
                )
            result, available_indices = cache.finding_results[result_id]
            if result.run_id in active or not indices <= available_indices:
                raise AnalysisError("source_unavailable")
            # Every incoming edge must retain the same exact source snapshots,
            # including edges to an ancestor already validated on another path.
            for entry in result.run.inputs:
                if inputs.get(entry["id"]) != entry:
                    raise AnalysisError("source_changed")
            if result.run_id not in heights:
                resolve_inputs(
                    result.run,
                    case,
                    require_current_review=False,
                    cache=cache,
                    finding_depth=depth + 1,
                )
                heights.setdefault(result.run_id, 0)
            height = max(height, 1 + heights[result.run_id])
            # A shared subtree may first be reached through a shorter path.
            if depth + height > 8:
                raise AnalysisError("source_unavailable")
        heights[run.pk] = height
    finally:
        active.remove(run.pk)

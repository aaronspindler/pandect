"""Find readable saved uses of address estimates in the selected findings."""

from django.contrib.postgres.expressions import ArraySubquery
from django.core.exceptions import ValidationError
from django.db.models import Func, JSONField, OuterRef, Q, Subquery
from django.db.models.functions import JSONObject
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.models import AnalysisReview, AnalysisRun


def verified_estimate_uses(queryset, case):
    from core.analysis.source_resolution import SourceResolutionCache, resolve_inputs

    # The saved review ID identifies the exact accepted estimate, including
    # historical single-candidate inputs that omitted a candidate index.
    candidate = queryset.filter(
        result_id=OuterRef("result_id"), item_index=OuterRef("item_index")
    ).order_by()
    uses = AnalysisRun.objects.filter(
        case=case,
        result__case=case,
        status__in=("complete", "partial", "empty"),
        inputs__contains=Func(
            JSONObject(review_id=OuterRef("pk")),
            function="jsonb_build_array",
            output_field=JSONField(),
        ),
    ).filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
    reviews = (
        AnalysisReview.objects.filter(
            case=case,
            result__case=case,
            result__run__case=case,
            result__run__operation="geocode_locations",
            decision=AnalysisReview.Decision.ACCEPTED,
            item_index__isnull=False,
        )
        .annotate(finding_id=Subquery(candidate.values("pk")[:1]))
        .filter(finding_id__isnull=False)
        .annotate(
            recent_uses=ArraySubquery(
                uses.order_by("-created_at", "-pk").values("pk")[:2]
            )
        )
        .order_by("-created_at", "pk")
        .values_list("result_id", "item_index", "finding_id", "recent_uses")[:32]
    )
    by_run = {}
    for result, index, finding, uses in reviews:
        for run_id in uses:
            by_run.setdefault(run_id, {})[str(result), index] = finding

    used = {}
    cache = SourceResolutionCache(case, with_content=False)
    for run in AnalysisRun.objects.filter(case=case, pk__in=by_run):
        try:
            sources = resolve_inputs(
                run, case, require_current_review=False, cache=cache
            )
        except AnalysisError, Http404, KeyError, TypeError, ValueError, ValidationError:
            continue
        if any(source.item.is_archived for source in sources):
            continue
        candidates = by_run[run.pk]
        for source in sources:
            finding = candidates.get(
                (source.derived_result_id, source.geocode_candidate)
            )
            if finding is not None:
                used.setdefault(finding, run.pk)
    return used

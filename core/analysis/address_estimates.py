"""Review and validate the retained revisions of analysis address estimates."""

from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.http import Http404

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.analysis.source_resolution import resolve_active_inputs
from core.models import AnalysisResult, AnalysisReview
from geo.coordinates import coordinate


def address_estimate(finding):
    """An address lookup can be selected only when it returned usable coordinates."""
    values = finding.get("values", {})
    try:
        return coordinate(values.get("latitude"), values.get("longitude"))
    except ValueError:
        return None


@transaction.atomic
def set_address_estimate(actor, case_id, run_id, item_index, *, use):
    case = get_case(actor, case_id, edit=True, lock=True)
    result = (
        AnalysisResult.objects.select_related("run")
        .filter(
            case=case,
            run_id=run_id,
            run__case=case,
            run__operation="geocode_locations",
            run__status__in=("complete", "partial"),
        )
        .filter(Q(run__previous_run__isnull=True) | Q(run__previous_run__case=case))
        .first()
    )
    if result is None:
        raise Http404("Address estimate not found.")
    items = result.data.get("items", [])
    if not 0 <= item_index < len(items) or address_estimate(items[item_index]) is None:
        raise Http404("Address estimate not found.")
    resolve_active_inputs(result.run, case, require_current_review=False)
    latest = result.reviews.filter(case=case, item_index=item_index).first()
    in_use = latest is not None and latest.decision == AnalysisReview.Decision.ACCEPTED
    if use != in_use:
        AnalysisReview.objects.create(
            case=case,
            result=result,
            reviewed_by=actor,
            decision=AnalysisReview.Decision.ACCEPTED
            if use
            else AnalysisReview.Decision.UNRESOLVED,
            item_index=item_index,
        )


def require_current_geocode_reviews(run, sources, *, exact_revision=False):
    """Check current acceptance after historical inputs have been resolved."""
    geocodes = {
        source.id for source in sources if source.derived_coordinates is not None
    }
    review_ids = {
        selected["review_id"] for selected in run.inputs if selected["id"] in geocodes
    }
    if not review_ids:
        return
    latest = AnalysisReview.objects.filter(
        case_id=run.case_id,
        result_id=OuterRef("result_id"),
        item_index=OuterRef("item_index"),
    ).order_by("-created_at", "id")
    pinned = AnalysisReview.objects.filter(
        pk__in=review_ids,
        case_id=run.case_id,
        result__case_id=run.case_id,
        result__run__case_id=run.case_id,
    )
    if exact_revision:
        accepted = pinned.filter(pk=Subquery(latest.values("pk")[:1])).count()
    else:
        accepted = (
            pinned.annotate(current_decision=Subquery(latest.values("decision")[:1]))
            .filter(current_decision=AnalysisReview.Decision.ACCEPTED)
            .count()
        )
    if accepted != len(review_ids):
        raise AnalysisError("geocode_review_required")

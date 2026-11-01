"""Analysis history removed with an explicitly deleted original."""

from django.contrib.admin.utils import NestedObjects
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from analysis.models import AnalysisRun
from cases.permissions import get_case

from .queries import get_evidence


def related_analysis_runs(item, kind):
    """Include entire multi-source runs and downstream results that depend on them."""
    runs = AnalysisRun.objects.filter(case_id=item.case_id)
    selected = runs.filter(
        inputs__contains=[{"kind": kind, "evidence_id": str(item.pk)}]
    )
    ids = set(selected.values_list("pk", flat=True))
    frontier = ids
    while frontier:
        result_ids = runs.filter(pk__in=frontier, result__isnull=False).values_list(
            "result__pk", flat=True
        )
        condition = Q(pk__in=[])
        for result_id in result_ids:
            condition |= Q(inputs__contains=[{"result_id": str(result_id)}])
        frontier = set(runs.filter(condition).values_list("pk", flat=True)) - ids
        ids.update(frontier)
    return runs.filter(pk__in=ids)


def deletion_block_reason(item, runs):
    """Inspect the actual cascade before displaying potentially private names."""
    collector = NestedObjects(using=item._state.db)
    collector.collect([item])
    if runs:
        collector.collect(runs)
    groups = [*collector.data.values(), collector.protected]
    # SET_NULL updates (for example previous_run) must stay in this case too.
    for updates in collector.field_updates.values():
        groups.extend(updates)
    if not all(
        getattr(related, "case_id", item.case_id) == item.case_id
        for group in groups
        for related in group
    ):
        return (
            "Related material has inconsistent case links. "
            "Ask an operator to correct them before deleting this evidence."
        )
    if collector.protected:
        return (
            "This original supports retained imported evidence and cannot be "
            "deleted individually. An operator must resolve its retained source "
            "relationships first."
        )
    return ""


@transaction.atomic
def delete_evidence(actor, case_id, kind, evidence_id):
    """Authorize and serialize the purge shared by product and admin workflows."""
    case = get_case(actor, case_id, lock=True)
    if not actor.is_superuser:
        raise PermissionDenied("Only superusers can permanently delete evidence.")
    item = get_evidence(case, kind, evidence_id, lock=True)
    runs = related_analysis_runs(item, kind)
    reason = deletion_block_reason(item, runs)
    if reason:
        raise ValidationError(reason)
    runs.delete()
    item.delete()

"""One same-case evidence boundary for readers, search, reports and workers."""

from django.db.models import F, Q
from django.http import Http404

from .models import (
    CommunicationEvidence,
    PersonLocationEvidence,
    VehicleLocationEvidence,
)
from .types import KINDS


def same_case_evidence(queryset):
    """Exclude inconsistent related records; callers still authorize case access."""
    model = queryset.model
    if issubclass(model, CommunicationEvidence):
        queryset = queryset.filter(
            sender__case_id=F("case_id"), recipient__case_id=F("case_id")
        )
    elif issubclass(model, PersonLocationEvidence):
        queryset = queryset.filter(person__case_id=F("case_id"))
    elif issubclass(model, VehicleLocationEvidence):
        queryset = queryset.filter(vehicle__case_id=F("case_id")).filter(
            Q(import_batch__isnull=True)
            | Q(
                import_batch__case_id=F("case_id"),
                import_batch__document__case_id=F("case_id"),
            )
        )
    return queryset


def scoped_evidence(case, kind):
    try:
        model = KINDS[kind]
    except KeyError:
        raise Http404("Evidence not found.") from None
    return same_case_evidence(model.objects.filter(case=case))


def get_evidence(case, kind, evidence_id, *, lock=False):
    queryset = scoped_evidence(case, kind)
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    try:
        return queryset.get(pk=evidence_id)
    except KINDS[kind].DoesNotExist, ValueError:
        raise Http404("Evidence not found.") from None

"""One same-case evidence boundary for readers, search, reports and workers."""

from django.db.models import Q
from django.http import Http404

from .types import COMMUNICATION_KINDS, KINDS


def scoped_evidence(case, kind):
    try:
        model = KINDS[kind]
    except KeyError:
        raise Http404("Evidence not found.") from None
    queryset = model.objects.filter(case=case)
    if kind in COMMUNICATION_KINDS:
        queryset = queryset.filter(sender__case=case, recipient__case=case)
    elif kind == "person-location":
        queryset = queryset.filter(person__case=case)
    elif kind == "vehicle-location":
        queryset = queryset.filter(vehicle__case=case).filter(
            Q(import_batch__isnull=True)
            | Q(import_batch__case=case, import_batch__document__case=case)
        )
    return queryset


def get_evidence(case, kind, evidence_id, *, lock=False):
    queryset = scoped_evidence(case, kind)
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    try:
        return queryset.get(pk=evidence_id)
    except KINDS[kind].DoesNotExist, ValueError:
        raise Http404("Evidence not found.") from None

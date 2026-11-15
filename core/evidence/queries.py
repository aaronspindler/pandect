"""One same-case evidence boundary for readers, search, reports and workers."""

from django.core.exceptions import ValidationError
from django.db.models import CharField, F, Q, Value
from django.http import Http404

from core.evidence.types import FILE_KINDS, KINDS
from geo.models import PersonLocationEvidence, VehicleLocationEvidence
from people.models import Person
from phone.models import CommunicationEvidence


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


def duplicate_matches(case, sha256):
    """Find exact file/captured-text originals, including archived records."""
    queries = [
        scoped_evidence(case, kind)
        .filter(sha256=sha256)
        .order_by()
        .annotate(kind=Value(kind, output_field=CharField()))
        .values("id", "title", "kind", "is_archived", "created_at")
        for kind in (*FILE_KINDS, "text")
    ]
    return (
        queries[0].union(*queries[1:], all=True).order_by("-created_at", "kind", "id")
    )


def get_evidence(case, kind, evidence_id, *, lock=False):
    queryset = scoped_evidence(case, kind)
    if lock:
        queryset = queryset.select_for_update(of=("self",))
    try:
        return queryset.get(pk=evidence_id)
    except KINDS[kind].DoesNotExist, ValueError:
        raise Http404("Evidence not found.") from None


def selected_person(case, value):
    """A supplied person filter must name a person in the authorized case."""
    if not value:
        return None
    try:
        return Person.objects.get(case=case, pk=value)
    except Person.DoesNotExist, ValueError, ValidationError:
        raise Http404("Person not found.") from None


def person_evidence(queryset, person):
    """Explicit relationships only; name/contact matches never imply identity."""
    queryset = same_case_evidence(queryset).filter(case_id=person.case_id)
    if issubclass(queryset.model, CommunicationEvidence):
        return queryset.filter(Q(sender=person) | Q(recipient=person))
    if issubclass(queryset.model, PersonLocationEvidence):
        return queryset.filter(person=person)
    return queryset.none()

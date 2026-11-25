"""One same-case evidence boundary for readers, search, reports and workers."""

import re
from datetime import UTC, date, datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db.models import CharField, Count, F, Q, Value
from django.http import Http404

from core.evidence.types import FILE_KINDS, KINDS
from geo.models import PersonLocationEvidence, VehicleLocationEvidence
from people.models import Person
from phone.models import CallEvidence, CommunicationEvidence, TextMessageEvidence


def same_case_evidence(queryset):
    """Exclude inconsistent related records; callers still authorize case access."""
    model = queryset.model
    if issubclass(model, CallEvidence):
        queryset = queryset.filter(
            Q(
                import_row__isnull=True,
                sender__case_id=F("case_id"),
                recipient__case_id=F("case_id"),
            )
            | (
                Q(
                    import_row__kind=model.communication_kind,
                    import_row__batch__case_id=F("case_id"),
                    import_row__batch__source__case_id=F("case_id"),
                    import_row__batch__document__case_id=F("case_id"),
                    import_row__batch__document__sha256=F(
                        "import_row__batch__source_sha256"
                    ),
                )
                & (Q(sender__isnull=True) | Q(sender__case_id=F("case_id")))
                & (Q(recipient__isnull=True) | Q(recipient__case_id=F("case_id")))
            )
        )
    elif issubclass(model, TextMessageEvidence):
        queryset = queryset.filter(sender__case_id=F("case_id")).filter(
            Q(group_source__isnull=True, recipient__case_id=F("case_id"))
            | Q(
                recipient__isnull=True,
                group_source__case_id=F("case_id"),
                communication_context__case_id=F("case_id"),
                communication_context__source_document_id=F("group_source_id"),
                communication_context__source_sha256=F("group_source__sha256"),
            )
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


def local_date_range(field, *, start=None, end=None):
    """Add indexable bounds around an exact local-calendar-date filter."""
    bounds = {}
    # A UTC offset is shorter than a day. Broad bounds keep the local date
    # exact even where a timezone transition changes or repeats midnight.
    day = timedelta(days=1)
    if start:
        bounds[f"{field}__date__gte"] = start
        if start > date.min:
            bounds[f"{field}__gte"] = datetime.combine(start - day, time.min, UTC)
    if end:
        bounds[f"{field}__date__lte"] = end
        if end < date.max - day:
            bounds[f"{field}__lt"] = datetime.combine(end + 2 * day, time.min, UTC)
    return bounds


def evidence_counts_by_type(case, *, archived):
    """Count visible evidence by type, including empty types, in one query."""
    queries = []
    for kind in KINDS:
        records = scoped_evidence(case, kind)
        if archived is not None:
            records = records.filter(is_archived=archived)
        queries.append(
            records.order_by()
            .annotate(kind=Value(kind))
            .values("kind")
            .annotate(total=Count("*"))
            .values_list("kind", "total")
        )
    return dict(queries[0].union(*queries[1:], all=True).order_by())


def matching_checksum(queryset, checksum):
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        return queryset.none()
    # The schema only permits lowercase digests. Use the existing Upper index
    # from case search for equality against the canonical input.
    return queryset.filter(sha256__iexact=checksum)


def duplicate_matches(case, sha256):
    """Find exact file or captured-text matches among active originals."""
    queries = [
        matching_checksum(scoped_evidence(case, kind), sha256)
        .filter(is_archived=False)
        .order_by()
        .annotate(kind=Value(kind, output_field=CharField()))
        .values("id", "title", "kind", "is_archived", "created_at")
        for kind in (*FILE_KINDS, "text")
    ]
    return (
        queries[0].union(*queries[1:], all=True).order_by("-created_at", "kind", "id")
    )


def get_evidence(case, kind, evidence_id, *, lock=False, identity_only=False):
    queryset = scoped_evidence(case, kind)
    if identity_only:
        queryset = queryset.only("pk")
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

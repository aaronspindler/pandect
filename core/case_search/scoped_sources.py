"""Date and type scopes for retained findings, excerpts and saved brief readers."""

from django.db.models import Q

from phone.models import CallEvidenceAttachment

from .retained import retained_passages
from .sources import dated, sources

BODY_FIELDS = {
    "text": "body",
    "message": "body",
    "attachment": "transcript",
    "extraction": "text",
    "finding": "preview",
}


def source_queryset(
    case,
    source,
    *,
    record_type="",
    start=None,
    end=None,
    through=None,
):
    def evidence(kind):
        queryset = dated(sources(case, kind), start, end)
        if record_type and record_type != kind:
            return queryset.none()
        return queryset

    if source in ("text", "message"):
        queryset = evidence("text" if source == "text" else "text-message")
    elif source == "attachment":
        clauses = Q(pk__in=[])
        for kind, parent in (("phone-call", "phone_call"), ("facetime", "facetime")):
            clauses |= Q(**{f"{parent}__in": evidence(kind).values("pk")})
        queryset = CallEvidenceAttachment.objects.filter(clauses, case=case).exclude(
            transcript=""
        )
    elif source == "extraction":
        queryset = retained_passages(case, record_type, originals=evidence)
    else:
        queryset = dated(sources(case, "finding"), start, end).exclude(
            Q(preview="") | Q(result__run__operation="sighting_group")
        )
        if record_type and record_type != "finding":
            return queryset.none()
    if through:
        field = (
            "result__created_at"
            if source in ("extraction", "finding")
            else "created_at"
        )
        queryset = queryset.filter(**{f"{field}__lte": through})
    return queryset

"""Resolve active original identities behind retained analysis passages."""

from django.db.models import Exists, OuterRef, Q

from core.analysis.catalogue import archived_finding_sources
from core.analysis.published import published_passages
from core.evidence.types import KINDS


def retained_passages(case, kind="", *, originals=None):
    queryset = published_passages(case)
    if kind:
        if kind not in KINDS:
            return queryset.none()
        queryset = queryset.filter(kind=kind)
    if originals:
        eligible = Q(pk__in=[])
        for candidate in (kind,) if kind else KINDS:
            current = originals(candidate).filter(
                pk=OuterRef("evidence_id"), sha256=OuterRef("evidence_sha256")
            )
            eligible |= Q(kind=candidate) & Exists(current)
        queryset = queryset.filter(
            eligible,
            ~Exists(
                archived_finding_sources(case).filter(
                    finding__result_id=OuterRef("result_id")
                )
            ),
        )
    return queryset


def resolved_source_identity(source):
    return (
        source.kind,
        source.item.pk,
        source.item.sha256,
        source.attachment.pk if source.attachment else None,
    )


def passage_identity(passage):
    return (
        passage.kind,
        passage.evidence_id,
        passage.evidence_sha256,
        passage.attachment_id,
    )

"""Search projections of exact retained extraction versions."""

from django.db.models import Exists, F, OuterRef, Q, Window
from django.db.models.functions import RowNumber
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.analysis.navigation import finding_url
from core.analysis.services import resolve_inputs
from core.evidence.search import content_excerpt
from core.models import ExtractedPassage


def derived_content_condition(queryset, *, case, kind, query):
    passages = ExtractedPassage.objects.filter(
        case=case,
        result__case=case,
        result__run__case=case,
        result__run__status__in=("complete", "partial"),
        kind=kind,
        evidence_id=OuterRef("pk"),
        evidence_sha256=OuterRef("sha256"),
        text__icontains=query,
    )
    return queryset.alias(matching_extraction=Exists(passages)), Q(
        matching_extraction=True
    )


def derived_text_matches(*, case, rows, query):
    if not query or not rows:
        return {}
    identities = Q(pk__in=[])
    for row in rows:
        identities |= Q(
            kind=row["kind"], evidence_id=row["id"], evidence_sha256=row["sha256"]
        )
    passages = (
        ExtractedPassage.objects.filter(
            identities,
            case=case,
            result__case=case,
            result__run__case=case,
            result__run__status__in=("complete", "partial"),
            text__icontains=query,
        )
        .annotate(
            candidate_number=Window(
                RowNumber(),
                partition_by=(F("kind"), F("evidence_id")),
                order_by=(F("result__created_at").desc(), F("pk").asc()),
            ),
            excerpt=content_excerpt("text", query),
        )
        .filter(candidate_number__lte=10)
        .select_related("result__run")
        .only(
            "kind",
            "evidence_id",
            "evidence_sha256",
            "attachment_id",
            "item_index",
            "result__run__inputs",
            "result__run__status",
        )
        .order_by("-result__created_at", "pk")
    )
    validated_runs = {}
    matches = {}
    for passage in passages:
        key = (passage.kind, passage.evidence_id)
        if key in matches:
            continue
        run = passage.result.run
        if run.pk not in validated_runs:
            try:
                sources = resolve_inputs(run, case)
            except AnalysisError, Http404:
                validated_runs[run.pk] = set()
            else:
                validated_runs[run.pk] = {
                    (
                        source.kind,
                        source.item.pk,
                        source.item.sha256,
                        source.attachment.pk if source.attachment else None,
                    )
                    for source in sources
                }
        identity = (
            passage.kind,
            passage.evidence_id,
            passage.evidence_sha256,
            passage.attachment_id,
        )
        if identity not in validated_runs[run.pk]:
            continue
        matches[key] = {
            "label": "Extracted content"
            + (" · Partial coverage" if run.status == "partial" else ""),
            "excerpt": passage.excerpt,
            "url": finding_url(case.pk, run.pk, passage.item_index),
        }
    return matches

"""Search projections of exact retained extraction versions."""

from django.db.models import Exists, OuterRef, Q

from .models import ExtractedPassage
from .navigation import finding_url


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
    if not query:
        return {}
    matches = {}
    for row in rows:
        passages = (
            ExtractedPassage.objects.filter(
                case=case,
                result__case=case,
                result__run__case=case,
                result__run__status__in=("complete", "partial"),
                kind=row["kind"],
                evidence_id=row["id"],
                text__icontains=query,
            )
            .select_related("result__run")
            .order_by("-result__created_at", "pk")[:10]
        )
        for passage in passages:
            # Revalidate the exact source version after SQL finds candidates.
            from .errors import AnalysisError
            from .services import resolve_inputs

            try:
                resolve_inputs(passage.result.run, case)
            except AnalysisError:
                continue
            position = passage.text.casefold().find(query.casefold())
            start = max(0, position - 60)
            matches[(row["kind"], row["id"])] = {
                "label": "Extracted content"
                + (
                    " · Partial coverage"
                    if passage.result.run.status == "partial"
                    else ""
                ),
                "excerpt": passage.text[start : start + 300],
                "url": finding_url(case.pk, passage.result.run_id, passage.item_index),
            }
            break
    return matches

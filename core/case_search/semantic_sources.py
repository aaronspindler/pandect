"""Current, inspectable natural-language sources for case passage retrieval."""

from collections import defaultdict

from django.contrib.postgres.search import SearchVector
from django.db.models import Case, F, Q, Value, When
from django.db.models.functions import Substr
from django.urls import reverse

from core.analysis.catalogue import finding_rows
from core.analysis.navigation import finding_url
from core.analysis.source_resolution import SourceResolutionCache
from core.evidence.types import KIND_LABELS
from core.models.search import SOURCE_FIELDS
from phone.conversations import message_url
from phone.models import CallEvidenceAttachment

from .documents import FIELDS, searchable, term_condition
from .retained import (
    finding_badges,
    passage_identity,
    retained_passages,
    valid_sources,
    visible_finding,
)
from .sources import dated, original_date_fields, sources

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
    exclusions=(),
    related=None,
    title_words=None,
):
    def evidence(kind):
        queryset = dated(sources(case, kind), start, end, kind)
        if record_type and record_type != kind:
            return queryset.none()
        if title_words is not None:
            queryset = queryset.alias(
                title_vector=SearchVector("title", config="simple")
            ).filter(title_vector=title_words)
        if exclusions:
            queryset = searchable(queryset, FIELDS[kind])
            for term in exclusions:
                queryset = queryset.exclude(related.condition(term, kind))
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
        queryset = dated(sources(case, "finding"), start, end, "finding").exclude(
            Q(preview="") | Q(result__run__operation="sighting_group")
        )
        if record_type and record_type != "finding":
            return queryset.none()
    if exclusions and source == "finding":
        queryset = searchable(queryset, FIELDS["finding"])
        for term in exclusions:
            queryset = queryset.exclude(term_condition(term))
    if through:
        field = (
            "result__created_at"
            if source in ("extraction", "finding")
            else "created_at"
        )
        queryset = queryset.filter(**{f"{field}__lte": through})
    return queryset


def document_source_condition(case, *, prefix="", **filters):
    """Match index documents whose underlying records remain eligible."""
    eligible = Q(pk__in=[])
    for source in SOURCE_FIELDS:
        originals = source_queryset(case, source, **filters).order_by().values("pk")
        eligible |= Q(**{f"{prefix}{source}_id__in": originals})
    return eligible


def _evidence_row(case, kind, item):
    date, date_label = next(
        (stamp, label)
        for field, label in original_date_fields(kind)
        if (stamp := getattr(item, field)) is not None
    )
    url = reverse("core:evidence:detail", args=(case.pk, kind, item.pk))
    if kind == "text-message":
        url = message_url(case, item)
    return {
        "id": item.pk,
        "kind": kind,
        "label": KIND_LABELS[kind],
        "title": item.title,
        "date": date,
        "date_label": date_label,
        "url": url,
        "badges": [],
        "terms": (),
    }


def read_sources(case, source, offsets, window, *, through=None, **filters):
    """Read one bounded window per source, with source links and current validity."""
    queryset = source_queryset(case, source, through=through, **filters).filter(
        pk__in=offsets
    )
    position = Case(
        *(When(pk=pk, then=Value(start + 1)) for pk, start in offsets.items())
    )
    queryset = queryset.annotate(window=Substr(BODY_FIELDS[source], position, window))
    if source in ("text", "message"):
        queryset = queryset.defer("body")
    elif source == "attachment":
        queryset = queryset.select_related("phone_call", "facetime").defer("transcript")
    elif source == "extraction":
        queryset = queryset.select_related("result__run").defer(
            "text", "result__data", "result__provenance"
        )
    elif source == "finding":
        queryset = queryset.annotate(
            excerpt=F("preview"), run_id=F("result__run_id")
        ).defer("search_text")
    items = list(queryset)
    if source == "finding":
        rows = finding_rows(case, items)
        return {
            item.pk: (
                item.window,
                {
                    "id": item.pk,
                    "kind": "finding",
                    "title": row["label"],
                    "label": "Finding",
                    "date": row["created_at"],
                    "date_label": "Analysis requested",
                    "url": row["url"],
                    "badges": finding_badges(row),
                    "terms": (),
                },
            )
            for item, row in zip(items, rows, strict=True)
            if visible_finding(row)
        }
    result = {}
    resolved = {}
    cache = SourceResolutionCache(case, with_content=False, active_only=True)
    for item in items:
        if source == "extraction":
            run = item.result.run
            if run.pk not in resolved:
                resolved[run.pk] = valid_sources(run, case, cache)
            original = resolved[run.pk].get(passage_identity(item))
            if original is None:
                continue
            row = _evidence_row(case, item.kind, original)
            row["url"] = finding_url(case.pk, run.pk, item.item_index)
            row["badges"].append("Extracted content")
            if run.status == "partial":
                row["badges"].append("Partial coverage")
        elif source == "attachment":
            kind = "phone-call" if item.phone_call_id else "facetime"
            row = _evidence_row(case, kind, item.call)
            row["url"] += f"#attachment-{item.pk}"
            row["badges"].append("Supplied transcript")
        else:
            kind = "text" if source == "text" else "text-message"
            row = _evidence_row(case, kind, item)
        result[item.pk] = item.window, row
    return result


def load_windows(case, requests, length, **filters):
    grouped = defaultdict(dict)
    for document, offset in requests:
        kind, pk = document.source()
        grouped[kind][pk] = offset
    return {
        (source, pk): item
        for source, offsets in grouped.items()
        for pk, item in read_sources(case, source, offsets, length, **filters).items()
    }

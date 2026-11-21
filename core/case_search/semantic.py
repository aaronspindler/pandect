"""Case-scoped exact vector retrieval with live source validation."""

import hashlib

from django.db import transaction
from django.db.models import F, Q, Window
from django.db.models.functions import RowNumber
from pgvector.django import CosineDistance

from core.analysis.errors import AnalysisError
from core.case_search.grammar import Query, parse_query
from core.case_search.indexing import authorize, search_index_status
from core.case_search.queries import RelatedMatches
from core.case_search.retained import RetainedMatches
from core.case_search.semantic_model import CHUNK_BYTES, embed, profile
from core.case_search.semantic_sources import load_windows, source_queryset
from core.models import SearchPassage
from core.models.search import SOURCE_FIELDS

# Qualification uses the pinned English model and compares against literal search.
MAX_DISTANCE = 0.55
RESULT_LIMIT = 6


def meaning_query(query, record_type):
    parsed = parse_query(query)
    if parsed.semantic_text and record_type not in {
        "person",
        "vehicle",
        "poi",
        "conversation",
        "trip",
    }:
        return parsed
    return None


def related_results(
    case,
    *,
    user,
    query,
    record_type="",
    state="active",
    start=None,
    end=None,
    keyword_urls=(),
):
    parsed = meaning_query(query, record_type)
    if parsed is None:
        return [], ""
    authorize(user.pk, case.pk)
    status = search_index_status(case, count_passages=False)
    if (
        status["status"] in ("disabled", "unprepared", "changed")
        or not status["passages"]
    ):
        return [], "" if status["status"] == "ready" else status["label"]
    active_profile = profile()
    try:
        (vector,) = embed([parsed.semantic_text], lambda: authorize(user.pk, case.pk))
    except AnalysisError:
        return [], "Meaning search is temporarily unavailable."
    filters = dict(
        record_type=record_type,
        state=state,
        start=start,
        end=end,
    )
    negative_terms = tuple(t for group in parsed.groups for t in group if t.negated)
    with transaction.atomic():
        retained = RetainedMatches(Query((negative_terms,)))
        if negative_terms and record_type != "finding":
            retained.index_passages(case, record_type, state, start, end)
        rows = _nearest(
            case,
            vector,
            active_profile,
            filters
            | {
                "exclusions": negative_terms,
                "related": RelatedMatches(case, retained),
            },
            keyword_urls,
        )
        retained.close()
    authorize(user.pk, case.pk)
    if profile() != active_profile:
        return [], "Meaning search settings changed. The index needs rebuilding."
    return rows, "" if status["status"] == "ready" else status["label"]


def _nearest(case, vector, active_profile, filters, keyword_urls):
    eligible = Q(pk__in=[])
    for source in SOURCE_FIELDS:
        sources = source_queryset(case, source, **filters)
        eligible |= Q(**{f"document__{source}_id__in": sources.order_by().values("pk")})
    choices = list(
        SearchPassage.objects.filter(
            eligible,
            case=case,
            document__case=case,
            document__profile=active_profile,
            document__complete=True,
        )
        .annotate(distance=CosineDistance("embedding", vector))
        .alias(
            document_rank=Window(
                RowNumber(),
                partition_by=F("document_id"),
                order_by=[F("distance").asc(), F("pk").asc()],
            )
        )
        .filter(distance__lte=MAX_DISTANCE, document_rank=1)
        .select_related("document")
        .defer("embedding")
        .order_by("distance", "pk")[:60]
    )
    windows = load_windows(
        case, [(p.document, p.start) for p in choices], CHUNK_BYTES, **filters
    )
    results = {}
    for passage in choices:
        found = windows.get(passage.document.source())
        if found is None:
            continue
        text, row = found
        if row["url"] in keyword_urls:
            continue
        excerpt = text[: passage.length]
        if hashlib.sha256(excerpt.encode()).hexdigest() != passage.digest:
            continue
        results.setdefault((row["kind"], row["id"]), row | {"excerpt": excerpt})
        if len(results) == RESULT_LIMIT:
            break
    return list(results.values())

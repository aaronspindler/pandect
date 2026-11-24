"""Case-scoped exact vector retrieval with live source validation."""

import hashlib

from django.db import transaction
from django.db.models import Case, F, Func, IntegerField, Q, When, Window
from django.db.models.functions import RowNumber
from django.db.models.lookups import Exact
from pgvector.django import CosineDistance

from core.analysis.errors import AnalysisError
from core.case_search.approval import search_approval_current
from core.case_search.grammar import Query, parse_query
from core.case_search.indexing import authorize, search_index_status
from core.case_search.queries import RelatedMatches
from core.case_search.retained import RetainedMatches
from core.case_search.semantic_model import CHUNK_BYTES, embed, is_external, profile
from core.case_search.semantic_sources import load_windows, source_queryset
from core.models import SearchPassage
from core.models.search import SOURCE_FIELDS

MAX_DISTANCE = 0.55
HOSTED_MIN_SIMILARITY = 0.4884
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
    allow_external=False,
    include_source=False,
):
    parsed = meaning_query(query, record_type)
    if parsed is None:
        return [], ""
    case = authorize(user.pk, case.pk)
    hosted = is_external()
    if hosted and not allow_external:
        return [], ""
    active_profile = profile()
    revision = (
        (case.search_embedding_approval or {}).get("revision") if hosted else None
    )

    def authorize_query():
        current = authorize(user.pk, case.pk)
        if profile() != active_profile or is_external() != hosted:
            raise AnalysisError("execution_changed")
        if hosted and (
            not revision or not search_approval_current(current, revision=revision)
        ):
            raise AnalysisError("embedding_approval_required")
        return current

    status = search_index_status(case, count_passages=False)
    if (
        status["status"] in ("disabled", "unprepared", "changed", "awaiting_approval")
        or not status["passages"]
    ):
        return [], "" if status["status"] == "ready" else status["label"]
    try:
        authorize_query()
        (vector,) = embed(
            [parsed.semantic_text], authorize_query, case=case, purpose="query"
        )
        authorize_query()
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
            1 - HOSTED_MIN_SIMILARITY if hosted else MAX_DISTANCE,
            include_source=include_source,
        )
        retained.close()
    try:
        authorize_query()
    except AnalysisError:
        return [], "Meaning search settings changed. Check the indexing status."
    return rows, "" if status["status"] == "ready" else status["label"]


def _nearest(
    case,
    vector,
    active_profile,
    filters,
    keyword_urls,
    max_distance,
    *,
    include_source=False,
):
    eligible = Q(pk__in=[])
    for source in SOURCE_FIELDS:
        sources = source_queryset(case, source, **filters)
        eligible |= Q(**{f"document__{source}_id__in": sources.order_by().values("pk")})
    # The planner may evaluate a distance predicate before filtering by profile.
    # CASE prevents pgvector from ever comparing two different vector widths.
    width = Func("embedding", function="vector_dims", output_field=IntegerField())
    distance = Case(
        When(Exact(width, len(vector)), then=CosineDistance("embedding", vector))
    )
    choices = list(
        SearchPassage.objects.filter(
            eligible,
            case=case,
            profile=active_profile,
            document__case=case,
            document__profile=active_profile,
            document__complete=True,
        )
        .annotate(distance=distance)
        .alias(
            document_rank=Window(
                RowNumber(),
                partition_by=F("document_id"),
                order_by=[F("distance").asc(), F("pk").asc()],
            )
        )
        .filter(distance__lte=max_distance, document_rank=1)
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
        result = row | {"excerpt": excerpt}
        if include_source:
            result.update(source=passage.document.source(), start=passage.start)
        results.setdefault((row["kind"], row["id"]), result)
        if len(results) == RESULT_LIMIT:
            break
    return list(results.values())

"""Case-scoped exact vector retrieval with live source validation."""

from itertools import batched, zip_longest

from django.db import transaction
from django.db.models import Case, F, Func, IntegerField, When, Window
from django.db.models.functions import RowNumber
from django.db.models.lookups import Exact
from pgvector.django import CosineDistance

from cases.permissions import get_case_for_verified_user
from core.analysis.errors import AnalysisError
from core.case_search.approval import search_approval_current
from core.case_search.grammar import Query, parse_query
from core.case_search.indexing import search_index_status
from core.case_search.parents import (
    anchor,
    current_parent_ids,
    parent_results,
    read_anchors,
    routing_terms,
)
from core.case_search.passage_relations import query_relations, rank_related
from core.case_search.queries import RelatedMatches
from core.case_search.retained import RetainedMatches
from core.case_search.semantic_model import embed, is_external, profile
from core.case_search.semantic_sources import document_source_condition
from core.models import SearchPassage

MAX_DISTANCE = 0.55
HOSTED_MIN_SIMILARITY = 0.4884
RESULT_LIMIT = 6
CANDIDATE_LIMIT = 24
CHAT_SOURCE_LIMIT = 32


def meaning_query(query, record_type):
    parsed = parse_query(query)
    if parsed.semantic_text and record_type not in {
        "person",
        "vehicle",
        "poi",
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
    start=None,
    end=None,
    keyword_urls=(),
    allow_external=False,
    embedding_revision=None,
    include_source=False,
    authorize_request=None,
    chat_turn=None,
    for_chat=False,
    through=None,
    parents_only=False,
    rank_scope=None,
):
    parsed = meaning_query(query, record_type)
    if parsed is None:
        return [], ""
    case = get_case_for_verified_user(user.pk, case.pk)
    hosted = is_external()
    active_profile = profile()
    revision = embedding_revision or (
        (case.search_embedding_approval or {}).get("revision") if hosted else None
    )

    def authorize_query():
        current = (
            authorize_request()
            if authorize_request
            else get_case_for_verified_user(user.pk, case.pk)
        )
        if current.pk != case.pk:
            raise AnalysisError("access_removed")
        if profile() != active_profile or is_external() != hosted:
            raise AnalysisError("execution_changed")
        if hosted and (
            not revision or not search_approval_current(current, revision=revision)
        ):
            raise AnalysisError("embedding_approval_required")
        return current

    vector = None
    notice = ""
    if active_profile and not parents_only and (not hosted or allow_external):
        status = search_index_status(case, count_passages=False)
        notice = "" if status["status"] == "ready" else status["label"]
        if status["passages"] and status["status"] not in (
            "disabled",
            "unprepared",
            "changed",
            "awaiting_approval",
        ):
            try:
                authorize_query()
                (vector,) = embed(
                    [parsed.semantic_text],
                    authorize_query,
                    case=case,
                    purpose="query",
                    chat_turn=chat_turn,
                )
                authorize_query()
            except AnalysisError:
                if authorize_request:
                    raise
                notice = "Meaning search is temporarily unavailable."
                vector = None
    filters = dict(record_type=record_type, start=start, end=end)
    negative_terms = tuple(t for group in parsed.groups for t in group if t.negated)
    with transaction.atomic():
        retained = RetainedMatches(Query((negative_terms,)))
        if negative_terms and record_type != "finding":
            retained.index_passages(case, record_type, start, end)
        filters.update(
            exclusions=negative_terms, related=RelatedMatches(case, retained)
        )
        chat = for_chat or chat_turn is not None
        relation = query_relations(parsed.semantic_text)
        limit = CANDIDATE_LIMIT if chat or rank_scope or relation else RESULT_LIMIT
        if chat and (
            len(routing_terms(parsed.semantic_text)) >= 3
            or getattr(chat_turn, "plan", {}).get("stage") == "refine"
        ):
            limit = CHAT_SOURCE_LIMIT
        cutoff = None if chat else 1 - HOSTED_MIN_SIMILARITY if hosted else MAX_DISTANCE
        parents = parent_results(
            case,
            user=user,
            query=parsed.semantic_text,
            vector=vector,
            active_profile=active_profile,
            through=through,
            max_distance=cutoff,
            **filters,
        )
        direct = []
        if vector is not None and record_type != "conversation":
            direct = _nearest(
                case,
                vector,
                active_profile,
                filters,
                keyword_urls,
                cutoff,
                display_distance=1 - HOSTED_MIN_SIMILARITY if hosted else MAX_DISTANCE,
                limit=limit,
                group_passages=chat or bool(relation),
                through=through,
            )
        if chat and relation:
            # A parent can introduce a different original at each fourth slot.
            # Compare dates/qualifiers on direct originals before those slots fill,
            # and retain the pure vector rank for the chat lane's reserved seats.
            direct_ranks = {}
            for row in direct:
                original = row["kind"], row["id"]
                row["direct_rank"] = direct_ranks.setdefault(
                    original, len(direct_ranks)
                )
            direct = rank_related(relation, direct)
        # Keep the head of the direct ranking while making room for original
        # sources discovered only by their saved summary or outline.
        merged = (
            row
            for group, parent in zip_longest(batched(direct, 3, strict=False), parents)
            for row in (*(group or ()), parent)
            if row and row["url"] not in keyword_urls
        )
        if relation and not chat:
            merged = rank_related(relation, list(merged))
            limit = CANDIDATE_LIMIT if rank_scope else RESULT_LIMIT
        rows = _by_original(merged, limit, keep_second=chat, secondary_last=chat)
        retained.close()
    if vector is not None:
        try:
            authorize_query()
        except AnalysisError:
            if authorize_request:
                raise
            return [], "Meaning search settings changed. Check the indexing status."
    if rank_scope and len(rows) > 1:
        from .rerank import rerank_order

        def recheck():
            current = get_case_for_verified_user(user.pk, case.pk)
            if not _current_excerpts(current, rows, record_type, start, end):
                raise AnalysisError("source_changed")
            return current

        order = rerank_order(
            case,
            query,
            [r["excerpt"] for r in rows],
            scope=rank_scope,
            authorize=recheck,
        )
        recheck()
        rows = [rows[n] for n in order][:RESULT_LIMIT]
    get_case_for_verified_user(user.pk, case.pk)
    if not include_source:
        rows = [
            {k: v for k, v in row.items() if k not in ("source", "start")}
            for row in rows
        ]
    return rows, notice


def _current_excerpts(case, rows, record_type, start, end):
    # Reread the eligible original before transferring an excerpt or showing
    # the provider's ordering. Only the original text is sent to the ranker.
    leads = {r["via_id"] for r in rows if "via_id" in r}
    if leads and current_parent_ids(case, leads) != leads:
        return False
    anchors = [anchor(*r["source"], r["start"], r["excerpt"]) for r in rows]
    current = read_anchors(
        case,
        anchors,
        filters={
            "record_type": "text-message"
            if record_type == "conversation"
            else record_type,
            "start": start,
            "end": end,
        },
    )
    return all(
        (a["source"], a["id"], a["start"], a["digest"]) in current for a in anchors
    )


def _by_original(rows, limit, *, keep_second, secondary_last=False):
    """Cap originals while preserving passage rank for relation scoring."""
    seen = {}
    ranked = []
    secondary = []
    for row in rows:
        original = row["kind"], row["id"]
        position = row["source"], row["start"]
        if original not in seen:
            if len(seen) == limit:
                continue
            seen[original] = {position}
            ranked.append(row)
        elif (
            keep_second and len(seen[original]) == 1 and position not in seen[original]
        ):
            seen[original].add(position)
            if secondary_last:
                secondary.append(row)
            else:
                ranked.append(row)
    return ranked + secondary


def _nearest(
    case,
    vector,
    active_profile,
    filters,
    keyword_urls,
    max_distance,
    *,
    display_distance,
    through=None,
    limit=RESULT_LIMIT,
    group_passages=False,
):
    eligible = document_source_condition(
        case, prefix="document__", through=through, **filters
    )
    # The planner may evaluate a distance predicate before filtering by profile.
    # CASE prevents pgvector from ever comparing two different vector widths.
    width = Func("embedding", function="vector_dims", output_field=IntegerField())
    distance = Case(
        When(Exact(width, len(vector)), then=CosineDistance("embedding", vector))
    )
    # Pages of one extraction and transcript additions to a single call must
    # not exhaust the wider chat shortlist before another original is read.
    partition = (
        [
            F("document__text_id"),
            F("document__message_id"),
            F("document__finding_id"),
            F("document__extraction__kind"),
            F("document__extraction__evidence_id"),
            F("document__attachment__phone_call_id"),
            F("document__attachment__facetime_id"),
        ]
        if group_passages
        else F("document_id")
    )
    choices = (
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
                partition_by=partition,
                order_by=[F("distance").asc(), F("pk").asc()],
            )
        )
        .filter(document_rank__lte=2 if group_passages else 1, distance__isnull=False)
        .select_related("document")
        .defer("embedding")
        .order_by("distance", "pk")
    )
    if max_distance is not None:
        choices = choices.filter(distance__lte=max_distance)
    choices = list(choices[: 2 * limit if limit > RESULT_LIMIT else 60])
    anchors = []
    for passage in choices:
        source, pk = passage.document.source()
        anchors.append(
            {
                "source": source,
                "id": str(pk),
                "start": passage.start,
                "length": passage.length,
                "digest": passage.digest,
            }
        )
    windows = read_anchors(case, anchors, filters=filters, through=through)
    results = []
    for a, passage in zip(anchors, choices, strict=True):
        row = windows.get((a["source"], a["id"], a["start"], a["digest"]))
        if row is not None and row["url"] not in keyword_urls:
            results.append(
                row
                | {
                    "via": "Original passage",
                    "strong_match": passage.distance <= display_distance,
                }
            )
    return _by_original(results, limit, keep_second=group_passages)

"""Find local outlines and resolve each lead to currently inspectable source text."""

import hashlib
import re
from collections import defaultdict
from uuid import UUID

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import (
    Case,
    Exists,
    F,
    Func,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    UUIDField,
    When,
)
from django.db.models.lookups import Exact
from pgvector.django import CosineDistance

from cases.permissions import get_case
from core.analysis.catalogue import SUCCESSFUL
from core.analysis.services import SourceResolutionCache
from core.case_search.grammar import TOKEN
from core.case_search.retained import valid_sources
from core.case_search.semantic_sources import read_sources, source_queryset
from core.evidence.queries import scoped_evidence
from core.models import AnalysisResult, AnalysisRun, SearchParent
from people.models import Person
from phone.saved_sessions import valid_sessions

VERSION = 2
MAX_ANCHORS = 10
EXCERPT = 240
STOP_WORDS = frozenset(
    (
        "a an and are at be by did do does for from how in is it of on or the "
        "this to was what when where which who with"
    ).split()
)


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def anchor(source, pk, offset, text):
    return {
        "source": source,
        "id": str(pk),
        "start": offset,
        "length": len(text),
        "digest": fingerprint(text),
    }


def read_anchors(case, anchors, *, filters=None, through=None):
    """Read multiple offsets of one source without overwriting earlier windows."""
    by_source = defaultdict(lambda: defaultdict(list))
    for ref in anchors:
        by_source[ref["source"]][UUID(ref["id"])].append(ref)
    resolved = {}
    for source, items in by_source.items():
        # Keep adjacent citation windows in one bounded database read. Large
        # gaps (e.g. document outlines) still fetch only the relevant regions.
        for refs in items.values():
            refs.sort(key=lambda ref: -ref["start"])
        while items:
            offsets = {}
            regions = {}
            width = 0
            for pk, refs in items.items():
                first = refs[-1]["start"]
                group = []
                while refs and refs[-1]["start"] - first < 8 * EXCERPT:
                    ref = refs.pop()
                    group.append(ref)
                    width = max(width, ref["start"] + ref["length"] - first)
                offsets[pk] = first
                regions[pk] = group
            current = read_sources(
                case, source, offsets, width, through=through, **(filters or {})
            )
            for pk, (window, row) in current.items():
                for ref in regions[pk]:
                    pos = ref["start"] - offsets[pk]
                    excerpt = window[pos : pos + ref["length"]]
                    if fingerprint(excerpt) == ref["digest"]:
                        resolved[_anchor_key(ref)] = row | {
                            "source": (source, pk),
                            "start": ref["start"],
                            "excerpt": excerpt,
                        }
            items = {pk: remaining for pk, remaining in items.items() if remaining}
    return resolved


def active_sessions(case):
    return valid_sessions(
        case,
        scoped_evidence(case, "text-message")
        .filter(is_archived=False)
        .exclude(communication_context__source_document__is_archived=True),
    )


def summary_sources(case):
    latest = (
        AnalysisRun.objects.filter(
            case=case,
            result__case=case,
            status__in=SUCCESSFUL,
            equivalence_key=OuterRef("run__equivalence_key"),
        )
        .filter(Q(previous_run__isnull=True) | Q(previous_run__case=case))
        .order_by("-created_at", "-pk")
        .values("pk")[:1]
    )
    return AnalysisResult.objects.filter(
        Q(run__operation="session_summaries")
        | Q(run__operation="text_topics", run__parameters___search_generation="briefs"),
        case=case,
        run__case=case,
        run_id=Subquery(latest),
    )


def _anchor_key(ref):
    return ref["source"], ref["id"], ref["start"], ref["digest"]


def _live_parents(case, parents, through):
    parents = list(parents)
    summary_ids = {p.summary_id for p in parents if p.summary_id}
    sessions = {p.session_id for p in parents if p.session_id}
    saved_ranges = {}
    if summary_ids:
        results = (
            summary_sources(case)
            .filter(pk__in=summary_ids)
            .select_related("run")
            .defer("data", "provenance")
        )
        if through:
            results = results.filter(created_at__lte=through)
        cache = SourceResolutionCache(case, with_content=False, active_only=True)
        for summary in results:
            if valid_sources(summary.run, case, cache):
                scope = summary.run.parameters.get("_saved_session_range")
                saved_ranges[summary.pk] = UUID(scope["session"]) if scope else None
        sessions.update(pk for pk in saved_ranges.values() if pk)
    if sessions:
        current = active_sessions(case).filter(pk__in=sessions)
        if through:
            current = current.filter(grouping__published_at__lte=through)
        sessions = set(current.values_list("pk", flat=True))
    usable_summaries = {
        pk
        for pk, session in saved_ranges.items()
        if session is None or session in sessions
    }
    checked = [
        p
        for p in parents
        if p.anchors
        and (not p.session_id or p.session_id in sessions)
        and (not p.summary_id or p.summary_id in usable_summaries)
    ]
    spans = {_anchor_key(r): r for p in checked for r in p.anchors}
    live = read_anchors(case, spans.values(), through=through)
    return [p for p in checked if all(_anchor_key(r) in live for r in p.anchors)], live


def current_parent_ids(case, ids, *, through=None):
    """Revalidate the representation's dependencies immediately before a transfer."""
    parents = SearchParent.objects.filter(case=case, pk__in=ids, version=VERSION).defer(
        "outline", "words", "embedding"
    )
    live, _ = _live_parents(case, parents, through)
    return {str(p.pk) for p in live}


def routing_terms(query):
    """Keep informative positive terms in longer chat questions and search phrases."""
    words = {}
    for match in TOKEN.finditer(query[:2000].replace(chr(0), " ")):
        term = match[3] if match[3] is not None else match[2]
        if term == "OR" or match[1] or term.startswith("-"):
            continue
        for word in re.findall(r"[^\W_]+", term.lower()):
            if word not in STOP_WORDS:
                words[word] = None
    return list(words)[:16]


class _ChildIds(Func):
    # Each outline names at most ten references. Probe their original UUIDs so
    # a selective search needn't materialize an array of every eligible source.
    template = (
        "(SELECT child.id FROM jsonb_to_recordset(%(expressions)s) "
        "AS child(id uuid, source text) WHERE child.source = '%(source)s')"
    )
    output_field = UUIDField()


def _scoped_candidates(case, parents, filters, *, title_words=None):
    eligible = Q(pk__in=[])
    # Only these four constant source kinds can occur in a parent anchor.
    for source in ("text", "message", "attachment", "extraction"):
        if title_words is not None and source == "message":
            continue
        rows = source_queryset(case, source, **filters, title_words=title_words)
        refs = _ChildIds(OuterRef("anchors"), source=source)
        eligible |= Exists(rows.filter(pk__in=refs))
    return parents.filter(eligible)


def parent_results(
    case,
    *,
    user,
    query,
    vector=None,
    active_profile="",
    record_type="",
    start=None,
    end=None,
    through=None,
    exclusions=(),
    related=None,
    limit=12,
    max_distance=None,
):
    """Bounded lexical and semantic parent discovery, yielding original child spans."""
    case = get_case(user, case.pk)
    if limit <= 0 or not query.strip():
        return []
    terms = routing_terms(query)
    if not terms:
        return []
    words = SearchQuery(terms[0], config="simple")
    for term in terms[1:]:
        words |= SearchQuery(term, config="simple")
    parents = SearchParent.objects.filter(case=case, version=VERSION)
    if record_type == "conversation":
        parents = parents.filter(label__in=("Conversation", "Conversation summary"))
        record_type = "text-message"
    filters = {
        "record_type": record_type,
        "start": start,
        "end": end,
        "exclusions": exclusions,
        "related": related,
    }
    scoped = any((record_type, start, end, exclusions))
    if through:
        # Each original predates its published summary, extraction or grouping.
        # Apply the turn cutoff without resolving every child of every outline.
        parents = parents.filter(
            Q(text__created_at__lte=through)
            | Q(attachment__created_at__lte=through)
            | Q(extraction__result__created_at__lte=through)
            | Q(summary__created_at__lte=through)
            | Q(session__grouping__published_at__lte=through)
        )
    candidates = _scoped_candidates(case, parents, filters) if scoped else parents
    lexical = (
        candidates.filter(words=words)
        .annotate(score=SearchRank(F("words"), words))
        .order_by("-score", "pk")
    )
    ranks = defaultdict(float)
    for rank, pk in enumerate(lexical.values_list("pk", flat=True)[:24], 1):
        ranks[pk] += 1 / (60 + rank)
    if record_type != "text-message":
        # Document outlines read editable titles from their current originals.
        matching_titles = _scoped_candidates(
            case,
            parents.filter(label__in=("Document outline", "Document section")),
            filters,
            title_words=words,
        ).order_by("pk")
        for rank, pk in enumerate(matching_titles.values_list("pk", flat=True)[:24], 1):
            ranks[pk] += 1 / (60 + rank)
    # Read participant names from the current case records, so renames never
    # leave old names in a persisted conversation outline.
    labels = [word for word in terms if len(word) > 2]
    if labels and record_type in ("", "text-message"):
        people = (
            Person.objects.filter(case=case)
            .filter(
                Q(
                    *(Q(first_name__iexact=t) | Q(last_name__iexact=t) for t in labels),
                    _connector=Q.OR,
                )
            )
            .order_by()
            .values("pk")
        )
        matching = candidates.filter(
            Q(session__grouping__first_person__in=people)
            | Q(session__grouping__second_person__in=people)
        ).order_by("-session__first_at", "pk")
        for rank, pk in enumerate(matching.values_list("pk", flat=True)[:12], 1):
            ranks[pk] += 1 / (60 + rank)
    if vector is not None and active_profile:
        width = Func(
            F("embedding"), function="vector_dims", output_field=IntegerField()
        )
        distance = Case(
            When(
                Exact(width, len(vector)),
                then=CosineDistance("embedding", list(vector)),
            )
        )
        semantic = (
            candidates.filter(profile=active_profile, embedding__isnull=False)
            .annotate(distance=distance)
            .filter(distance__isnull=False)
            .order_by("distance", "pk")
        )
        if max_distance is not None:
            semantic = semantic.filter(distance__lte=max_distance)
        for rank, pk in enumerate(semantic.values_list("pk", flat=True)[:16], 1):
            ranks[pk] += 1 / (60 + rank)
    order = sorted(ranks, key=ranks.get, reverse=True)[:24]
    found = parents.defer("embedding", "outline", "words").in_bulk(order)
    # Verify every dependency before an outline can influence retrieval. Apply
    # explicit result filters to returned children separately (a conversation may
    # legitimately span two dates).
    candidates, live = _live_parents(
        case, [found[pk] for pk in order if pk in found], through
    )
    unique = {_anchor_key(ref): ref for p in candidates for ref in p.anchors}
    eligible = (
        read_anchors(case, unique.values(), filters=filters, through=through)
        if scoped
        else live
    )
    output = {}
    for parent in candidates:
        keys = [_anchor_key(r) for r in parent.anchors]
        hits = [(k, eligible[k]) for k in keys if k in eligible]
        hits.sort(key=lambda pair: -sum(t in pair[1]["excerpt"].lower() for t in terms))
        for key, row in hits[:2]:
            output.setdefault(
                key[:3],
                row
                | {
                    "via": parent.label,
                    "via_id": str(parent.pk),
                    "badges": [
                        *row["badges"],
                        parent.label,
                        *(
                            ["Partial coverage"]
                            if parent.coverage.get("partial")
                            else []
                        ),
                    ],
                },
            )
            if len(output) >= min(limit, 24):
                return list(output.values())
    return list(output.values())

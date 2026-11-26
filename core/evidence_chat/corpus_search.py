"""Rank evidence windows using case record fields and the private passage index."""

import re
from collections import defaultdict
from functools import cache, reduce
from itertools import islice, zip_longest
from operator import or_

from django.conf import settings
from django.db.models import Case, Q, TextField, Value, When
from django.db.models.functions import Cast, Lower, StrIndex, Substr

from core.case_search.documents import FIELDS, searchable, term_condition
from core.case_search.grammar import Term
from core.case_search.queries import id_match
from core.case_search.semantic import (
    CANDIDATE_LIMIT,
    CHAT_SOURCE_LIMIT,
    related_results,
)
from core.evidence.search import CALL_HISTORY_SEARCH_FIELDS
from core.evidence.types import CALL_KINDS, COMMUNICATION_KINDS, FILE_KINDS
from text.analysis.synthesis import finding_text

from .corpus import (
    body_field,
    clipped,
    material,
    original_keys,
    read_window,
    rows,
    surfaces,
)
from .corpus_navigation import nearby_sources
from .passage_windows import WINDOW, lexical_spans, promote_relations, window_offsets
from .search_budget import MAX_CANDIDATES, timed_query
from .search_scope import scope_rows
from .spelling import alternatives, original_span

STOPWORDS = frozenset(
    "a an and any are as at about across all be by can case could did do does "
    "evidence every find for from has have how i in into is it me mentions "
    "mention of on or our records regarding say says show tell than that the "
    "their them there these this to was we were what when where which who "
    "with would you know please overview summarize summarise summary overall "
    "key facts give brief available main this far so".split()
)


# A direct request for a record UUID is satisfied by indexed primary-key reads.
# Questions about relationships involving that UUID still use the hybrid search.
RECORD_LOOKUP = re.compile(
    r"(?:please\s+)?(?:(?:show|find|open|locate)\s+(?:me\s+)?)?(?:the\s+)?"
    r"(?:(?:record|evidence|item)\s+)?"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})[.?!]?",
    re.I,
)


def terms_for(question, *, extra_stopwords=()):
    words = re.findall(r"[^\W_]+(?:[-@.][^\W_]+)*", question.lower())
    return list(
        dict.fromkeys(
            word
            for word in words
            if word not in STOPWORDS and word not in extra_stopwords
        )
    )[:8]


def fields_for(surface, kind):
    if surface == "passage":
        return ("text",)
    if surface == "finding":
        return ("search_text",)
    if surface == "attachment":
        return ("transcript", "original_filename")
    fields = ["title", "source", "source_url"]
    if kind in FILE_KINDS:
        fields.append("original_filename")
    if kind in ("text", "text-message"):
        fields.append("body")
    if kind in COMMUNICATION_KINDS:
        for party in ("sender", "recipient"):
            fields.extend(
                f"{party}__{field}"
                for field in ("first_name", "last_name", "email", "phone_number")
            )
        fields.append("occurred_at")
    if kind in CALL_KINDS:
        fields.extend(CALL_HISTORY_SEARCH_FIELDS)
    if kind.endswith("location"):
        fields.extend(("address", "latitude", "longitude", "observed_at"))
        if kind == "vehicle-location":
            fields.extend(
                (
                    "vehicle__make",
                    "vehicle__model",
                    "vehicle__license_plate",
                    "vehicle__vin",
                    "raw_row",
                )
            )
        if kind == "person-location":
            fields.extend(
                (
                    "person__first_name",
                    "person__last_name",
                    "person__email",
                    "person__phone_number",
                )
            )
    return fields


def matching_rows(queryset, surface, kind, terms, content, limit):
    index_fields = FIELDS[kind if surface == "original" else surface]
    queryset = searchable(queryset, index_fields)
    fields = fields_for(surface, kind)
    extra = [f for f in fields if f not in index_fields]
    conditions = []
    weights = []
    for word in terms:
        term = Term(word, False, False)
        # Use the same indexed expression as Case Search. Confirm matches
        # against the chat-visible fields before loading any original window.
        native = term_condition(term)
        if set(index_fields) - set(fields):
            native &= reduce(
                or_,
                (Q(**{f"{f}__icontains": word}) for f in fields if f in index_fields),
            )
        condition = native | id_match(word)
        for field in extra:
            if field.endswith("_at") and not term.literal:
                continue
            condition |= Q(**{field + "__icontains": word})
        conditions.append(condition)
        weights.append(3 if term.literal else 1)
    if conditions:
        score = sum(
            Case(When(condition, then=weight), default=0)
            for condition, weight in zip(conditions, weights, strict=True)
        )
        queryset = queryset.filter(reduce(or_, conditions)).annotate(score=score)
    else:
        queryset = queryset.annotate(score=Value(0))
    if content and terms:
        # Each source may match via metadata alone. Keep 0 as that sentinel.
        field = Lower(Cast(content, TextField()))
        offsets = [StrIndex(field, Value(term)) for term in terms]
        queryset = queryset.annotate(
            **{f"match_{i}": offset for i, offset in enumerate(offsets)}
        )
    return queryset.order_by("-score", "pk")[:limit]


LEXICAL_LIMIT = 10
RRF_K = 15


@timed_query()
def _lexical(case, terms, include_archived, through, filters, selected, after=None):
    groups = []
    for surface, kind in selected:
        field = body_field(surface, kind)
        kind_words = set(kind.split("-")) if surface == "original" else set()
        keywords = [t for t in terms if t.removesuffix("s") not in kind_words]
        matches = matching_rows(
            scope_rows(
                rows(case, surface, kind, include_archived, through, after=after),
                case,
                surface,
                kind,
                filters,
            ),
            surface,
            kind,
            keywords,
            field,
            2 * LEXICAL_LIMIT,
        )
        offsets = [f"match_{i}" for i in range(len(keywords))] if field else []
        group = [
            (
                score + (len(keywords) < len(terms)),
                (surface, kind, str(pk)),
                tuple(
                    (term, n - 1)
                    for term, n in zip(keywords, positions, strict=False)
                    if n
                ),
            )
            for pk, score, *positions in matches.values_list("pk", "score", *offsets)
        ]
        groups.append(group)
    if not terms:
        return [hit for group in zip_longest(*groups) for hit in group if hit]
    return sorted(
        (hit for group in groups for hit in group),
        key=lambda hit: (-hit[0], hit[1][0] != "original", hit[1]),
    )


def _meaning(case, question, user, related, through, filters):
    if related is not None:
        results, _ = related(question)
    else:
        # Direct corpus callers can use the private index. Hosted dispatch always
        # goes through the turn's submitted destination and query authorization.
        if settings.PANDECT_SEARCH_EMBEDDING_PROVIDER != "local":
            return
        query = " ".join(re.findall(r"\w+", question)[:16])[:200]
        if not query:
            return
        results, _ = related_results(
            case,
            user=user,
            query=query,
            include_source=True,
            for_chat=True,
            through=through,
            **(filters or {}),
        )
    for result in islice(results, 2 * CHAT_SOURCE_LIMIT):
        source, pk = result["source"]
        if source == "finding":
            surface, kind = "finding", ""
        elif source == "extraction":
            surface, kind = "passage", ""
        elif source == "attachment":
            surface, kind = "attachment", result["kind"]
        else:
            surface, kind = "original", result["kind"]
        yield (surface, kind, str(pk)), result


def ranked_windows(
    case,
    question,
    user,
    include_archived,
    limit,
    through,
    *,
    after=None,
    related=None,
    hints=(),
    stamp_body=False,
    filters=None,
    semantic_hints=None,
    surfaces_to_search=None,
    stats=None,
):
    limit = min(limit, CANDIDATE_LIMIT)
    if limit <= 0:
        return []
    terms = terms_for(question)
    # Typed lookups read only the requested families. Hybrid search can also
    # use the semantic index, spelling leads and implicit source links.
    hybrid = surfaces_to_search is None and after is None
    selected = tuple(
        pair
        for pair in surfaces()
        if surfaces_to_search is None or pair[0] in surfaces_to_search
    )
    if not selected:
        return []
    allowed = set(selected)

    if direct_id := RECORD_LOOKUP.fullmatch(question.strip()):
        found = []
        for surface, kind in selected:
            qs = scope_rows(
                rows(case, surface, kind, include_archived, through, after=after),
                case,
                surface,
                kind,
                filters,
            )
            item = clipped(
                qs.filter(pk=direct_id[1]), body_field(surface, kind), review=stamp_body
            ).first()
            if item is not None:
                if stats is not None:
                    stats["candidates"] = stats.get("candidates", 0) + 1
                hit = material(
                    case, surface, kind, item, include_archived, through=through
                )
                if hit and read_window(
                    case, hit.locator, 0, 1, include_archived=include_archived
                ):
                    found.append((hit.locator, 0))
                    if len(found) >= limit:
                        break
        return found
    meaning = (
        list(_meaning(case, question, user, related, through, filters))
        if hybrid
        else []
    )
    # Retrieve each group of shortlisted originals in one scoped query. Keep
    # only the requested substrings, lengths and hashes of long text bodies.
    candidate_rows = {}
    candidate_starts = defaultdict(dict)
    admitted = set()
    for (surface, kind, pk), hit in meaning:
        key = (surface, kind, pk)
        if (
            surface == "original"
            and kind in ("text", "text-message")
            and len(admitted) < CHAT_SOURCE_LIMIT
        ):
            admitted.add(key)
            candidate_starts[kind].setdefault(pk, hit["start"])
    for kind, positions in candidate_starts.items():
        queryset = scope_rows(
            rows(case, "original", kind, include_archived, through),
            case,
            "original",
            kind,
            filters,
        )
        offsets = Case(
            *(When(pk=pk, then=Value(start + 1)) for pk, start in positions.items()),
            default=1,
        )
        for item in clipped(
            queryset.filter(pk__in=positions), "body", review=stamp_body
        ).annotate(indexed_excerpt=Substr("body", offsets, WINDOW)):
            candidate_rows[("original", kind, str(item.pk))] = item

    @cache
    def load(key):
        surface, kind, pk = key
        if (surface, kind) not in allowed:
            return None
        if key not in admitted and len(admitted) >= MAX_CANDIDATES:
            if stats is not None:
                stats["limited"] = True
            return None
        admitted.add(key)
        if key in candidate_rows:
            item = candidate_rows[key]
        else:
            qs = scope_rows(
                rows(case, surface, kind, include_archived, through, after=after),
                case,
                surface,
                kind,
                filters,
            ).filter(pk=pk)
            item = clipped(qs, body_field(surface, kind), review=stamp_body).first()
        if item is None:
            return None
        found = material(case, surface, kind, item, include_archived, through=through)
        if not found or found.locator["gap"] in {
            "Original file is unavailable.",
            "Original text failed its integrity check.",
        }:
            return None
        return item, found.locator

    # Score a source once per lane. Keep each lane's offset when the lanes agree
    # about the source but point to different statements in a long original.
    scores = defaultdict(float)
    spans = defaultdict(list)
    for _, key, positions in _lexical(
        case, terms, include_archived, through, filters, selected, after
    ):
        if not (source := load(key)):
            continue
        item, loc = source
        if key[0] == "finding":
            body = finding_text(item.canonical_item).lower()
            positions = [(t, body.find(t)) for t in terms]
        offset = loc.get("metadata_start", 0) if terms else 0
        spans[key] = lexical_spans(item, body_field(*key[:2]), terms, positions) or [
            (offset, offset + 1)
        ]
        scores[key] = 1 / (RRF_K + len(scores) + 1)
        if len(scores) == LEXICAL_LIMIT:
            break
    lexical_order = list(scores)
    direct_ranks = {}
    strong_spans = defaultdict(list)
    seen_meaning = set()
    for key, result in meaning:
        if not (source := load(key)):
            continue
        item, loc = source
        offset = result["start"]
        if key[0] == "finding":
            offset = finding_text(item.canonical_item).find(result["excerpt"])
        excerpt = result["excerpt"]
        if (
            not isinstance(excerpt, str)
            or not excerpt
            or not 0 <= offset < loc["length"]
        ):
            continue
        sample_start = (
            candidate_starts[key[1]].get(key[2]) if key in candidate_rows else None
        )
        relative = offset - sample_start if sample_start is not None else -1
        if relative >= 0 and relative + len(excerpt) <= len(item.indexed_excerpt):
            if not item.indexed_excerpt.startswith(excerpt, relative):
                continue
        else:
            current = read_window(
                case, loc, offset, len(excerpt), include_archived=include_archived
            )
            if current is None or current["text"] != excerpt:
                continue
        # A generated outline can lead to an original only while its own
        # dependencies remain current. Carry that dependency across worker retries.
        if parent := result.get("via_id"):
            leads = loc.setdefault("search_parents", [])
            if parent not in leads:
                leads.append(parent)
        else:
            direct_ranks.setdefault(key, result.get("direct_rank", len(direct_ranks)))
            # An outline can introduce a candidate but a model-free relevance
            # decision needs a qualified score on the verified child itself.
            if semantic_hints is not None and result.get("strong_match"):
                strong_spans[key].append((offset, offset + len(excerpt)))
        if key not in seen_meaning:
            weight = 1.5 if result.get("strong_match") else 1
            scores[key] += weight / (RRF_K + len(seen_meaning) + 1)
            seen_meaning.add(key)
            spans[key].insert(0, (offset, offset + len(excerpt)))
        else:
            spans[key].append((offset, offset + len(excerpt)))

    if hybrid and (variants := alternatives(case, question, terms, through)):
        added = 0
        for _, key, positions in islice(
            _lexical(case, variants, include_archived, through, filters, selected), 12
        ):
            if key[0] == "finding" or not (source := load(key)):
                continue
            if span := original_span(
                case, *source, positions, variants, include_archived
            ):
                added += 1
                spans[key].append(span)
                scores[key] += 1 / (RRF_K + 3 + added)
                if added == 3:
                    break

    starts = {key: window_offsets(value) for key, value in spans.items()}
    ranked = sorted(scores, key=lambda k: -scores[k])
    if len(ranked) > CHAT_SOURCE_LIMIT:
        originals, repeats, covered = [], [], set()
        for key in ranked:
            origins = original_keys(load(key)[1])
            if origins - covered:
                originals.append(key)
                covered.update(origins)
            else:
                repeats.append(key)
        ranked = (originals + repeats)[:CHAT_SOURCE_LIMIT]
    ranked, starts = promote_relations(
        case, question, ranked, starts, load, include_archived
    )
    suggestions = []
    for hint, start in islice(hints, 4):
        surface = hint["surface"]
        kind = hint["kind"] if surface in ("original", "attachment") else ""
        key = (surface, kind, hint["pk"])
        if (
            (source := load(key))
            and source[1]["version"] == hint["version"]
            and 0 <= start < source[1]["length"]
            and read_window(case, hint, start, 1, include_archived=include_archived)
        ):
            suggestions.append((key, start))
    primaries = [(key, starts[key][0]) for key in ranked]
    # Immediately read a cited original or a definite reply when one of the best
    # hits points there. Neighbours are search leads and retain their own labels.
    neighbours = []
    cited_primaries = []
    anchors = (suggestions + primaries)[:2] if hybrid else ()
    for key, _ in anchors:
        item, loc = load(key)
        for neighbour in nearby_sources(
            case, question, item, loc, include_archived, through, filters=filters
        ):
            if neighbour != key and load(neighbour):
                choice = neighbour, starts.get(neighbour, [0])[0]
                if choice in neighbours:
                    continue
                neighbours.append(choice)
                if key[0] == "finding":
                    cited_primaries.append(choice)
                if len(neighbours) == 2:
                    break
        if len(neighbours) == 2:
            break

    choices = (
        primaries[:1] + suggestions[:2] + primaries[1:2] + neighbours + primaries[2:]
    )
    independent, rest = [], []
    covered = set()
    for choice in [*cited_primaries, *choices]:
        # Prefer a finding's cited original when independent accounts fill the
        # budget. The derived interpretation can still use a spare slot.
        origins = original_keys(load(choice[0])[1])
        if origins - covered:
            independent.append(choice)
            covered.update(origins)
        else:
            rest.append(choice)
    # A second distant hit in the strongest text may contain a correction or the
    # answer itself. Preserve room for additional independent sources.
    second = [
        (key, pos)
        for key in ranked
        for pos in starts[key][1:]
        if abs(pos - starts[key][0]) >= WINDOW
    ]
    # Give each lane representation when overlap elsewhere inflates fused
    # scores. Keep the fused leaders, including citations and verified links.
    direct_order = sorted(direct_ranks, key=direct_ranks.get)
    heads = set(lexical_order[:2] + direct_order[:5])
    protected = [choice for choice in independent if choice[0] in heads]
    leaders = set(lexical_order[:1] + direct_order[:1])
    protected.sort(key=lambda choice: choice[0] not in leaders)
    choices = (
        independent[:4] + second[:1] + protected + independent[4:] + rest + second[1:]
    )
    selected = []
    used = defaultdict(list)
    for key, start in choices:
        if len(used[key]) >= 2 or any(abs(start - n) < WINDOW for n in used[key]):
            continue
        loc = load(key)[1]
        if read_window(case, loc, start, 1, include_archived=include_archived) is None:
            continue
        used[key].append(start)
        selected.append((loc, start))
        if semantic_hints is not None and any(
            left < start + WINDOW and right > start for left, right in strong_spans[key]
        ):
            semantic_hints.add((loc["key"], start))
        if len(selected) == limit:
            break
    if stats is not None:
        stats["candidates"] = len(admitted)
    return selected

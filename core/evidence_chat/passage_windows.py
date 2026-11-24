"""Locate answer-bearing spans in bounded samples of shortlisted long sources."""

import re
from collections import defaultdict

from django.db.models import Case, Value, When
from django.db.models.functions import Substr

WINDOW = 900
SAMPLE = 2 * WINDOW
SAMPLE_COUNT = 10


def _best_span(matches):
    latest = {}
    best = None
    # At most eight query terms. Their latest occurrences give the tightest
    # span ending at this match; no pairs of occurrences need to be enumerated.
    for begin, end, term in sorted(matches, key=lambda hit: hit[1]):
        latest[term] = begin
        covered = [start for start in latest.values() if end - start <= WINDOW]
        if covered:
            left = min(covered)
            candidate = (-len(covered), end - left, left, end)
            if best is None or candidate < best:
                best = candidate
    return best[2:] if best else None


def _text_matches(samples, terms):
    matches = set()
    for offset, text in samples:
        for term in terms:
            for match in re.finditer(re.escape(term), text, re.I):
                matches.add((offset + match.start(), offset + match.end(), term))
    return sorted(matches)


def lexical_spans(item, field, terms, first_hits):
    """Read at most 10 distributed samples plus one per matching query term."""
    if not terms:
        return [(0, 1)]
    first = [
        (start, start + len(term), term) for term, start in first_hits if start >= 0
    ]
    if not first:
        return []
    length = getattr(item, "body_length", 0)
    if field and length > WINDOW:
        if length <= SAMPLE_COUNT * SAMPLE:
            offsets, size = [0], length
        else:
            last = length - SAMPLE
            offsets = sorted(
                {
                    *(i * last // (SAMPLE_COUNT - 1) for i in range(SAMPLE_COUNT)),
                    *(min(last, max(0, start - WINDOW)) for start, _, _ in first),
                }
            )
            size = SAMPLE
        # The caller just hydrated this exact case-scoped candidate. A concurrent
        # change is caught by the original version checks before inference.
        slices = (
            type(item)
            .objects.filter(pk=item.pk, case_id=item.case_id)
            .values_list(*(Substr(field, offset + 1, size) for offset in offsets))
            .first()
        )
        if slices is not None:
            first = _text_matches(zip(offsets, slices, strict=True), terms)
    matches = sorted(first)
    spans = []
    for _ in range(2):
        span = _best_span(matches)
        if not span:
            break
        spans.append(span)
        start = max(0, span[0] - 100, span[1] - WINDOW)
        matches = [m for m in matches if m[1] <= start or m[0] >= start + WINDOW]
    return spans


def window_offsets(spans):
    """Fit the strongest spans into two nonoverlapping excerpts."""
    groups = []
    for start, end in spans:
        for i, (left, right) in enumerate(groups):
            joined = min(left, start), max(right, end)
            if joined[1] - joined[0] <= WINDOW:
                groups[i] = joined
                break
        else:
            if len(groups) < 2:
                groups.append((start, end))

    def positioned(span):
        start, end = span
        return max(0, start - 100, end - WINDOW)

    offsets = [positioned(group) for group in groups]
    if len(groups) == 2:
        early, late = sorted(range(2), key=lambda i: groups[i][0])
        if offsets[early] + WINDOW > offsets[late]:
            # Adjacent excerpts cover both spans even if the spans overlap.
            offsets[early] = max(0, min(offsets[early], groups[late][0] - WINDOW))
            offsets[late] = max(offsets[early] + WINDOW, positioned(groups[late]))
    return offsets


def promote_relations(case, question, ranked, starts, load, include_archived):
    """Prefer an original window that binds an action to its date or qualifier."""
    from core.case_search.passage_relations import query_relations, relation_strength

    from .corpus import body_field, clipped, rows

    spec = query_relations(question)
    if not spec:
        return ranked, starts
    groups = defaultdict(dict)
    strengths = {}
    for key in ranked[:32]:
        item, _ = load(key)
        if getattr(item, "body_length", 0):
            groups[key[:2]][item.pk] = key, item.body_digest
    for (surface, kind), items in groups.items():
        field = body_field(surface, kind)
        qs = rows(case, surface, kind, include_archived).filter(pk__in=items)
        # Read the two shortlisted original windows in one statement per source
        # type. A label or collection date appended by chat is outside the body.
        slices = []
        for index in range(max(len(starts[key]) for key, _ in items.values())):
            offsets = Case(
                *(
                    When(pk=pk, then=Value(starts[key][index] + 1))
                    for pk, (key, _) in items.items()
                    if index < len(starts[key])
                )
            )
            slices.append(Substr(field, offsets, WINDOW))
        for pk, digest, *texts in clipped(qs, field).values_list(
            "pk", "body_digest", *slices
        ):
            key, expected_digest = items[pk]
            if digest != expected_digest:
                continue
            options = [
                (relation_strength(spec, text), offset)
                for offset, text in zip(starts[key], texts, strict=False)
                if text
            ]
            if options:
                strength, start = max(options, key=lambda pair: pair[0])
                strengths[key] = strength
                if strength:
                    starts[key] = list(dict.fromkeys([start, *starts[key]]))
    return [
        key
        for _, key in sorted(
            enumerate(ranked), key=lambda hit: hit[0] - 4 * strengths.get(hit[1], 0)
        )
    ], starts

"""Case-local spelling leads from structured person and place names."""

import re
from collections import defaultdict

from django.db.models import Q, TextField, Value
from django.db.models.functions import Concat, Left

from core.case_search.documents import FIELDS, searchable
from core.case_search.sources import sources

from .corpus import original_metadata, read_window

# Adjacent punctuation in emails, hashtags, URLs, codes or filenames keeps the
# entire token literal. Ordinary sentence punctuation can still follow a name.
WORD = re.compile(r"(?<![\w.@/+_#-])[^\W\d_]{5,40}(?![\w@/+_#-]|\.[\w])")
LITERAL = re.compile(
    r'["“”`@#]|\b(?:plate|VIN|identifier|serial|tracking|code|hash|SHA\d*|'
    r"email|username|phone|UUID|postcode|ID)\b",
    re.I,
)
NAME_CONTEXT = re.compile(
    r"\b(?:named?|called|near|from|at|in|with|person|place|street|who is)\s+$", re.I
)
NAMES = {
    "person": ("first_name", "last_name"),
    "poi": ("custom_label", "address"),
    "location": ("address",),
    "person-location": ("address",),
    "vehicle-location": ("address",),
}


def _one_edit(left, right):
    """One insertion, deletion, substitution or adjacent transposition."""
    if len(left) > len(right):
        left, right = right, left
    if len(right) - len(left) > 1:
        return False
    i = next(
        (i for i, (a, b) in enumerate(zip(left, right, strict=False)) if a != b),
        len(left),
    )
    if len(left) != len(right):
        return left[i:] == right[i + 1 :]
    return i < len(left) and (
        left[i + 1 :] == right[i + 1 :]
        or left[i : i + 2] == right[i : i + 2][::-1]
        and left[i + 2 :] == right[i + 2 :]
    )


def alternatives(case, question, terms, through):
    if LITERAL.search(question):
        return []
    words = list(
        dict.fromkeys(
            m[0].lower()
            for m in WORD.finditer(question)
            if not m[0].isupper()
            and m[0].lower() in terms
            and (
                m[0].istitle()
                and (m.start() or len(question.split()) == 1)
                or NAME_CONTEXT.search(question[: m.start()])
            )
        )
    )[:3]
    if not words:
        return []
    # A single edit preserves either the start or end. The functional uppercase
    # trigram indexes already used by Case Search accelerate these predicates.
    candidates = Q(pk__in=[])
    labels_match = Q(pk__in=[])
    for word in words:
        n = max(3, (len(word) - 1) // 2)
        for fragment in (word[:n], word[-n:]):
            candidates |= Q(search_document__icontains=fragment)
            labels_match |= Q(names__icontains=fragment)
    lookups = []
    for kind, fields in NAMES.items():
        qs = sources(case, kind).queryset
        if through:
            qs = qs.filter(
                **{
                    "updated_at__lte"
                    if kind in ("person", "poi")
                    else "created_at__lte": through
                }
            )
        labels = (
            fields[0]
            if len(fields) == 1
            else Concat(fields[0], Value(" "), fields[1], output_field=TextField())
        )
        qs = (
            searchable(qs, FIELDS[kind])
            .filter(candidates)
            .annotate(names=Left(labels, 2000))
            .filter(labels_match)
        )
        lookups.append(qs.order_by("pk").values_list("names", flat=True)[:12])
    options = defaultdict(set)
    for label in lookups[0].union(*lookups[1:], all=True).order_by():
        for value in WORD.findall(label.lower()):
            for word in words:
                if value == word or _one_edit(value, word):
                    options[word].add(value)
    return [
        next(iter(options[w]))
        for w in words
        if len(options[w]) == 1 and w not in options[w]
    ][:2]


def original_span(case, item, loc, positions, words, include_archived):
    """Return only an exact word from original content or reported metadata."""
    for word, pos in positions:
        left = max(0, pos - 1)
        chunk = read_window(
            case, loc, left, len(word) + 3, include_archived=include_archived
        )
        if chunk and any(
            m.start() == pos - left and m[0].lower() == word
            for m in WORD.finditer(chunk["text"])
        ):
            return pos, pos + len(word)
    if loc["surface"] == "original":
        metadata, _, _ = original_metadata(item, loc["kind"])
        offset = loc["metadata_start"]
        for line in metadata.splitlines(keepends=True):
            field, _, value = line.partition(": ")
            if field in {
                "Address",
                "Reported sender",
                "Reported recipient",
                "Reported person",
            }:
                for match in WORD.finditer(value):
                    if match[0].lower() in words:
                        start = offset + len(field) + 2 + match.start()
                        return start, start + len(match[0])
            offset += len(line)
    return None

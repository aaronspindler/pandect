"""A bounded, literal-preserving query language for case records."""

import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError

TOKEN = re.compile(r'(-?)"([^"]*)(?:"|$)|([^\s"]+)')
IDENTIFIER = re.compile(
    r"[^\w\s]|\d|_|^(?:[a-f]{32}|[a-f]{40}|[a-f]{64})$", re.IGNORECASE
)


@dataclass(frozen=True)
class Term:
    text: str
    negated: bool
    quoted: bool

    @property
    def literal(self):
        return self.quoted or bool(IDENTIFIER.search(self.text))


@dataclass(frozen=True)
class Query:
    groups: tuple

    @property
    def terms(self):
        return tuple(
            dict.fromkeys(t.text for g in self.groups for t in g if not t.negated)
        )

    @property
    def exclusions(self):
        return tuple(dict.fromkeys(t.text for g in self.groups for t in g if t.negated))

    @property
    def semantic_text(self):
        if len(self.groups) != 1:
            return ""
        terms = [term for term in self.groups[0] if not term.negated]
        if any(term.quoted for term in terms) or all(term.literal for term in terms):
            return ""
        return " ".join(term.text for term in terms)


def parse_query(value):
    if len(value) > 200 or "\0" in value:
        raise ValidationError(
            "Enter a search of up to 200 characters without null characters."
        )
    groups = [[]]
    for match in TOKEN.finditer(value):
        word = match[3]
        if word == "OR":
            if groups[-1]:
                groups.append([])
            continue
        quoted = word is None
        negated = bool(match[1]) if quoted else word.startswith("-") and len(word) > 1
        text = match[2] if quoted else word[1:] if negated else word
        if text.strip():
            groups[-1].append(Term(text, negated, quoted))
    if sum(map(len, groups)) > 16:
        raise ValidationError("Use up to 16 search terms.")
    return Query(
        tuple(tuple(group) for group in groups if any(not t.negated for t in group))
    )


def highlight_terms(value):
    return parse_query(value).terms

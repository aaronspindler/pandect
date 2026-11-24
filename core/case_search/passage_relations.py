"""Rank original wording that connects an action to the requested time or qualifier."""

import re
from dataclasses import dataclass
from itertools import pairwise

# Normalize common event verbs without conflating an event with its later report.
# Other verbs keep their literal wording.
ACTIONS = {
    "occur": r"happen(?:ed|s)?|occur(?:red|s)?|took\s+place",
    "capture": r"photograph(?:ed|ing)|snapp(?:ed|ing)|captur(?:e|ed|ing)|taken|took",
    "send": r"shar(?:e|ed|ing)|send(?:s|ing)?|sent|forward(?:ed|ing)?|transmit(?:ted)?",
    "record": r"writ(?:e|ing|ten)|wrote|typ(?:e|ed|ing)|recorded|documented|logged",
    "leave": r"depart(?:ure|ed|ing)?|leav(?:e|ing)|left",
    "arrive": r"arriv(?:e|al|ed|ing)|reached",
    "correct": r"correct(?:ed|ion)?|revis(?:e|ed|ion)|amend(?:ed|ment)?|changed",
    "estimate": r"estimat(?:e|ed|ing)|guess(?:ed)?",
}
ACTION = re.compile(
    r"\b(?:"
    + "|".join(f"(?P<{name}>{pattern})" for name, pattern in ACTIONS.items())
    + r")\b",
    re.I,
)
VISUAL = re.compile(
    r"\b(?:photo(?:graph)?s?|images?|pictures?|videos?|footage|shots?)\b", re.I
)
MODIFIER = re.compile(
    r"\b(?:(?P<approx>approximate(?:ly)?|around|roughly|"
    r"about(?=\s+\d)|maybe|sometime|best guess)"
    r"|(?P<negative>never|not|no longer|cannot|\w+n['’]t)"
    r"|(?P<when>\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]m)?|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|yesterday|today|"
    r"morning|afternoon|evening|midnight|noon))\b",
    re.I,
)
TIME_CUE = re.compile(r"\b(?:when|time|dated?|before|after|earlier|later|then)\b", re.I)
SEPARATOR = re.compile(r"[,;.!?\n]+|\b(?:and|but|whereas|while)\b", re.I)
VERB = re.compile(r"\b[a-z]{3,}(?:ed|ing)\b", re.I)
ORDER = re.compile(r"\b(before|earlier than|after|later than)\b", re.I)


@dataclass(frozen=True)
class Event:
    action: str
    start: int
    end: int
    times: frozenset
    qualifiers: frozenset


def _events(text, extra):
    actions = []
    for m in ACTION.finditer(text):
        if m.group().lower() in {"took", "taken"} and not VISUAL.search(
            text, max(0, m.start() - 60), m.end() + 60
        ):
            continue
        actions.append((m.start(), m.end(), m.lastgroup))
    actions.extend(
        (m.start(), m.end(), m.group().lower())
        for m in VERB.finditer(text)
        if m.group().lower() in extra
    )
    actions.sort()
    marks = [[] for _ in actions]
    for marker in MODIFIER.finditer(text):
        # Assign each date/qualifier to one action within the same clause.
        closest = None
        for i, (start, end, _) in enumerate(actions):
            left, right = (
                (end, marker.start())
                if end <= marker.start()
                else (min(marker.end(), start), start)
            )
            distance = right - left
            if distance <= 72 and not SEPARATOR.search(text, left, right):
                if closest is None or distance < closest[0]:
                    closest = distance, i
        if closest is not None:
            marks[closest[1]].append(marker)
    return [
        Event(
            role,
            start,
            end,
            frozenset(m.group().lower() for m in group if m.lastgroup == "when"),
            frozenset(m.lastgroup for m in group if m.lastgroup != "when"),
        )
        for (start, end, role), group in zip(actions, marks, strict=True)
    ]


@dataclass(frozen=True)
class RelationQuery:
    events: tuple
    extra_verbs: frozenset
    order: tuple


def _ordered_pair(events, text):
    for first, second in pairwise(events):
        if first.action == second.action:
            continue
        between = text[first.end : second.start]
        if len(between) > 120 or re.search(r"[.;!?\n]", between):
            continue
        if marker := ORDER.search(between):
            if marker[0].lower() in ("before", "earlier than"):
                return first.action, second.action
            return second.action, first.action
    return ()


def query_relations(question):
    """Recognize bounded event/date or event/qualifier requests."""
    text = question[:1200]
    if not (MODIFIER.search(text) or TIME_CUE.search(text)):
        return None
    extra = frozenset(
        m[0].lower()
        for m in VERB.finditer(text)
        if m[0].lower()
        not in {"during", "regarding", "according", "something", "anything"}
        and not ACTION.fullmatch(m[0])
    )
    events = tuple(_events(text, extra)[:4])
    return RelationQuery(events, extra, _ordered_pair(events, text)) if events else None


def relation_strength(spec, text):
    """Score the strongest relationship within one short stretch of original text."""
    matches = []
    for candidate in _events(text, spec.extra_verbs):
        for wanted in spec.events:
            if candidate.action != wanted.action:
                continue
            if ("negative" in wanted.qualifiers) != (
                "negative" in candidate.qualifiers
            ):
                continue
            if (
                not wanted.times <= candidate.times
                or not wanted.qualifiers <= candidate.qualifiers
            ):
                continue
            exact = bool(wanted.times or wanted.qualifiers)
            if exact or candidate.times or wanted.action in spec.order:
                matches.append(
                    (candidate, (wanted.action, wanted.times, wanted.qualifiers))
                )
                break

    best = 0
    requested = {(w.action, w.times, w.qualifiers) for w in spec.events}
    for i, (first, _) in enumerate(matches):
        supported = set()
        exact = False
        local = []
        for candidate, relation in matches[i:]:
            if (
                candidate.end - first.start > 340
                or "\n\n" in text[first.start : candidate.end]
            ):
                break
            if relation in supported:
                continue
            local.append(candidate)
            supported.add(relation)
            exact |= bool(relation[1] or relation[2])
            # An incidental date on just one verb is weak evidence for a
            # relationship between two events. Keep the original ranking.
            if len(requested) > 1 and len(supported) == 1 and not exact:
                continue
            strength = len(supported) + int(exact)
            if spec.order and set(spec.order) <= {e.action for e in local}:
                order = _ordered_pair(local, text)
                if order and order != spec.order:
                    continue
                strength += int(bool(order))
            best = max(best, strength)
    return best


def rank_related(spec, rows):
    if not spec:
        return rows
    # The relation only adjusts relevance. Keep competing accounts in range.
    return [
        row
        for _, row in sorted(
            enumerate(rows),
            key=lambda hit: hit[0] - 4 * relation_strength(spec, hit[1]["excerpt"]),
        )
    ]

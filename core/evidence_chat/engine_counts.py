"""Count explicit structured record filters using reported case data."""

import re
from datetime import date
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from core.case_search.sources import name
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, COMMUNICATION_KINDS, LOCATION_KINDS
from people.models import Person

KINDS = {
    "text messages": ["text-message"],
    "messages": ["text-message"],
    "texts": ["text-message"],
    "phone calls": ["phone-call"],
    "facetime calls": ["facetime"],
    "calls": list(CALL_KINDS),
    "images": ["image"],
    "photos": ["image"],
    "documents": ["document"],
    "recordings": ["audio"],
    "audio recordings": ["audio"],
    "vehicle locations": ["vehicle-location"],
    "person locations": ["person-location"],
    "location records": ["location"],
    "text records": ["text"],
}
QUERY = re.compile(
    r"(?:how many|(?:what is |what's )?(?:the )?(?:total )?(?:number|count) of"
    r"|count(?: all)?|(?:show|list|find) (?:me )?(?:all|every)) "
    r"(?P<kind>" + "|".join(KINDS) + r")"
    r"(?: (?:are (?:there|recorded)|do we have|were recorded))?"
    r"(?: (?:in|for|across) (?:the|this|my) case)?"
    r"(?: between (?P<first>[^?.,]+?) and (?P<second>[^?.,]+?))?"
    r"(?: on (?P<day>\d{4}-\d{2}-\d{2}|monday|tuesday|wednesday|thursday"
    r"|friday|saturday|sunday))?"
    r"[?.!]*",
    re.I,
)
DAYS = set("monday tuesday wednesday thursday friday saturday sunday".split())

# Whole-question matching keeps qualifications, negation and unquoted concepts
# on the interpretive route. A literal always fits inside the review overlap.
LITERAL_QUERY = re.compile(
    r"(?:please\s+)?(?:list|find|show|identify)\s+(?:me\s+)?(?:every|all(?:\s+the)?)\s+"
    r"(?P<active>active\s+)?records?\s+(?:that\s+|which\s+)?(?:mention|contain)(?:s|ing)?\s+"
    r'(?:"(?P<quoted>[^"\r\n]{1,80})"|“(?P<curly>[^”\r\n]{1,80})”|'
    r"'(?P<single>[^'\r\n]{1,80})'|‘(?P<curly_single>[^’\r\n]{1,80})’|"
    r"(?P<name>[^\W\d_][\w’'-]{0,79})\s+by\s+name)[.!?]?",
    re.I,
)


def literal_question(turn):
    match = LITERAL_QUERY.fullmatch(turn.question.strip())
    if match and not (turn.include_archived and match["active"]):
        literal = (
            match["quoted"]
            or match["curly"]
            or match["single"]
            or match["curly_single"]
            or match["name"]
        )
        return literal if literal.strip() else ""
    return ""


def literal_outcomes(term, windows):
    left = r"(?<!\w)" if re.match(r"\w", term[0]) else ""
    right = r"(?!\w)" if re.match(r"\w", term[-1]) else ""
    pattern = re.compile(left + re.escape(term) + right, re.I)
    outcomes = []
    for w in windows:
        refs = []
        for match in pattern.finditer(w.text):
            if left and not match.start() and w.start:
                continue
            if (
                right
                and match.end() == len(w.text)
                and w.start + match.end() < w.locator["length"]
            ):
                continue
            start, end = w.start + match.start(), w.start + match.end()
            if not any(
                first <= start and end <= last for first, last in w.literal_ranges
            ):
                continue
            refs.append(
                {
                    "start": start,
                    "end": end,
                    "quote": match[0],
                }
            )
        outcomes.append(("match" if refs else "miss", refs))
    return outcomes


def _people(case, value):
    value = value.strip().strip('"“”')
    return list(
        Person.objects.filter(case=case)
        .alias(label=name())
        .filter(
            Q(label__iexact=value)
            | Q(email__iexact=value)
            | Q(phone_number__iexact=value)
            | Q(first_name__iexact=value)
            | Q(last_name__iexact=value)
        )
        .values_list("pk", flat=True)[:2]
    )


def structured_question(turn, question):
    """Return an executable filter or a focused clarification for ambiguous fields."""
    match = QUERY.fullmatch(" ".join(question.split()))
    if not match:
        return None, ""
    kinds = KINDS[match["kind"].lower()]
    filters = {"kinds": kinds}
    if match["first"]:
        if not set(kinds) <= set(COMMUNICATION_KINDS):
            return None, ""
        parties = []
        for key in ("first", "second"):
            ids = _people(turn.case, match[key])
            if not ids:
                # A phrase beyond the recognized grammar (e.g. 'and B about X')
                # belongs in a content review; never silently discard its condition.
                return None, ""
            if len(ids) > 1:
                return (
                    None,
                    f"Which person do you mean by “{match[key]}”? "
                    "Give their full recorded name or identifier.",
                )
            parties.append(str(ids[0]))
        filters["parties"] = parties
    if match["day"]:
        day = match["day"].lower()
        if day in DAYS:
            return None, f"Which calendar date do you mean by {day.title()}?"
        try:
            filters["day"] = date.fromisoformat(day).isoformat()
        except ValueError:
            return None, "Which valid calendar date should I use?"
        filters["timezone"] = (
            turn.plan.get("timezone") or turn.asked_by.timezone or "UTC"
        )
    return filters, ""


def evaluate(case, sources, filters):
    """Classify each frozen original, using case-enforced participant relationships."""
    result = {}
    for kind in filters["kinds"]:
        ids = [source.evidence_id for source in sources if source.evidence_kind == kind]
        if not ids:
            continue
        qs = scoped_evidence(case, kind).filter(pk__in=ids)
        if "parties" in filters:
            a, b = filters["parties"]
            qs = qs.filter(
                Q(sender_id=a, recipient_id=b) | Q(sender_id=b, recipient_id=a)
            )
        if "day" not in filters:
            result.update((pk, "match") for pk in qs.values_list("pk", flat=True))
            continue
        date_field = (
            "occurred_at"
            if kind in COMMUNICATION_KINDS
            else "observed_at"
            if kind in LOCATION_KINDS
            else "collected_at"
        )
        zone = ZoneInfo(filters["timezone"])
        day = date.fromisoformat(filters["day"])
        for pk, timestamp in qs.values_list("pk", date_field):
            if timestamp is None:
                result[pk] = "uncertain"
            elif timezone.localdate(timestamp, zone) == day:
                result[pk] = "match"
    return result

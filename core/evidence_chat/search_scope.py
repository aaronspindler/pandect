"""Apply unambiguous investigator-specified type and date filters on every pass."""

import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.evidence.source_clocks import is_date_only_message
from core.evidence.types import KINDS
from core.models import ChatTurn

QUOTED_TEXT = re.compile(r"""“[^”]*”|‘[^’]*’|"[^"]*"|(?<!\w)'[^']*'(?!\w)""")
TYPE = re.compile(r"(?<!\S)type:([\w-]+)\b", re.I)
DATE = re.compile(r"(?<!\S)(from|through|on):(\d{4}-\d{2}-\d{2})\b", re.I)
FIELDS = {"passage": "extraction", "finding": "finding"}
REQUEST_PREFIX = r"^\s*(?:(?:please|can you|could you)\s+)?"
ENUMERATED = re.compile(
    REQUEST_PREFIX
    + r"(?P<after>after\s+)?(?:check|review|search|inspect)(?P<gerund>ing)?\s+"
    r"(?:all(?:\s+the)?|every|each)\s+"
    r"(?P<population>records?|sources?|(?:text\s+)?messages?|items?)\b",
    re.I,
)
FULL_REVIEW = re.compile(
    REQUEST_PREFIX + r"(?:(?:do|run|start|perform)\s+)?(?:a\s+)?(?:full|exhaustive)\s+"
    r"(?:review|search)\b"
    r"(?:\s+(?:of|through|across)\s+(?:(?:all(?:\s+the)?|every|each|the)\s+)?"
    r"(?P<messages>(?:text\s+)?messages?)\b)?",
    re.I,
)
FULL_REVIEW_VERSION = 3


def review_population(question, *, direct_only=False):
    """Identify the population the investigator explicitly asked chat to review."""
    question = QUOTED_TEXT.sub(" ", question)
    if match := ENUMERATED.match(question):
        if direct_only and (match["after"] or match["gerund"]):
            return ""
        return "text-message" if "message" in match["population"].lower() else "all"
    if not direct_only and (match := FULL_REVIEW.match(question)):
        return "text-message" if match["messages"] else "all"
    return ""


def normalize_date_filters(question):
    return re.sub(r"\b(on|from|through)\s+(?=\d{4}-\d)", r"\1:", question, flags=re.I)


def search_filters(question, timezone_name):
    # A date inside a quotation or in the narrative may be a claim to verify.
    # Only a filter explicitly supplied by the investigator constrains retrieval.
    query = QUOTED_TEXT.sub(" ", question)
    selected = {}
    match = TYPE.search(query)
    if match and match[1].lower() in (*KINDS, "finding"):
        selected["record_type"] = match[1].lower()
    for mode, day in DATE.findall(query):
        try:
            value = date.fromisoformat(day)
        except ValueError:
            continue
        zone = ZoneInfo(timezone_name)
        if mode.lower() in ("from", "on"):
            selected["start"] = datetime.combine(value, time.min, zone)
        if mode.lower() in ("through", "on") and value < date.max:
            selected["end"] = datetime.combine(
                value + timedelta(days=1), time.min, zone
            )
    return selected


def question_filters(turn):
    return search_filters(turn.question, turn.plan.get("timezone", "UTC"))


def preceding_review_clarification(turn):
    from . import engine

    if (
        turn.ordinal <= 1
        or engine.NEW_QUESTION.match(turn.question)
        or turn.question.rstrip().endswith("?")
        or engine.review_requested(turn.question)
    ):
        return None
    previous = (
        ChatTurn.objects.filter(
            case_id=turn.case_id,
            chat_id=turn.chat_id,
            ordinal=turn.ordinal - 1,
            status="complete",
            mode="clarification",
        )
        .only("question", "answer", "plan")
        .first()
    )
    if previous and engine.review_requested(previous.question):
        return previous
    return None


def review_restart_code(turn):
    if turn.error_code == "chat_review_scope_retired":
        return turn.error_code
    version = turn.plan.get("review_version")
    if turn.mode != "full" or version == FULL_REVIEW_VERSION:
        return ""
    if version == 2:
        # Keep published replies inspectable. An interrupted scan must not
        # combine prior matches with the new scope or literal rules.
        if turn.status == "complete":
            return ""
        if turn.plan.get("literal"):
            return "chat_literal_review_retired"
        counts = turn.plan.get("filters", {})
        if question_filters(turn) or (
            "day" in counts and "text-message" in counts.get("kinds", ())
        ):
            return "chat_review_scope_retired"
        return ""
    if review_population(turn.question) == "text-message":
        if version != 1 or question_filters(turn).get("record_type") != "text-message":
            return "chat_review_scope_retired"
    previous = preceding_review_clarification(turn)
    if previous and review_population(previous.question) == "text-message":
        if (
            version != 1
            or turn.plan.get("scoped_clarification") != str(previous.pk)
            or question_filters(previous).get("record_type") != "text-message"
            or question_filters(turn).get("record_type", "text-message")
            != "text-message"
        ):
            return "chat_review_scope_retired"
    if version == 1:
        return ""
    if question_filters(turn):
        return "chat_review_scope_retired"
    if turn.plan.get("literal"):
        return "chat_literal_review_retired"
    return ""


def dated_originals(case, kind, filters):
    from core.case_search.sources import dated, sources

    start, end = filters.get("start"), filters.get("end")
    matches = dated(sources(case, kind), start, end)
    if kind == "text-message" and (start or end):
        matches = matches.filter(~is_date_only_message(case))
    return matches


def scope_rows(queryset, case, surface, kind, filters, *, include_recordings=False):
    if not filters:
        return queryset
    from core.case_search.scoped_sources import source_queryset

    if surface in ("original", "attachment"):
        if filters.get("record_type", kind) != kind:
            return queryset.none()
        matches = dated_originals(case, kind, filters).order_by().values("pk")
        if surface == "original":
            return queryset.filter(pk__in=matches)
        parent = "phone_call" if kind == "phone-call" else "facetime"
        queryset = queryset.filter(**{f"{parent}__in": matches})
        return queryset if include_recordings else queryset.exclude(transcript="")
    matches = source_queryset(case, FIELDS[surface], **filters).order_by().values("pk")
    return queryset.filter(pk__in=matches)


def dependencies_in_scope(case, locator, filters):
    """Require derived context to respect the scope of its contributing originals."""
    surface = locator["surface"]
    if surface not in ("finding", "passage"):
        return True
    dates = {key: filters[key] for key in ("start", "end") if filters.get(key)}
    record_type = filters.get("record_type") if surface == "passage" else None
    if not (dates or record_type):
        return True
    inputs = defaultdict(set)
    for dep in locator["dependencies"]:
        kind = dep["kind"]
        if kind not in KINDS or record_type and kind != record_type:
            return False
        inputs[kind].add(dep["id"])
    return bool(inputs) and all(
        dated_originals(case, kind, dates).filter(pk__in=ids).count() == len(ids)
        for kind, ids in inputs.items()
    )


def context_source_allowed(case, locator, filters):
    """Check the active turn's scope before a locator can supply model context."""
    surface = locator["surface"]
    if (
        surface == "record"
        and locator.get("record_surface") == "record"
        and locator.get("omit_notes") is not True
    ):
        return False
    if not filters:
        return True

    from . import corpus

    kind = locator["kind"] if surface in ("original", "attachment") else ""
    if (surface, kind) not in corpus.surfaces():
        # Aggregate reports, trips and mutable records cannot certify an
        # explicit evidence filter from the bounded locator alone.
        return False
    rows = corpus.rows(case, surface, kind, False, retained=True)
    return dependencies_in_scope(case, locator, filters) and (
        scope_rows(rows, case, surface, kind, filters, include_recordings=True)
        .filter(pk=locator["pk"])
        .exists()
    )

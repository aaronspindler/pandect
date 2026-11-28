"""Deterministic boundaries shared by selected analyses and paged session builds."""

import re
from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist

from core.evidence.source_clocks import message_event_time
from text.analysis.shared import STOPWORDS, WORD

SESSION_RULES_VERSION = "conversation-rules-v3"
REPLY_WINDOW = timedelta(hours=4)
WORDING_LIMIT = 8192


def message_context(message):
    try:
        return message.communication_context
    except AttributeError, ObjectDoesNotExist:
        return None


def participant_stream(message, *, include_reported_pair=True):
    pair = tuple(sorted((str(message.sender_id), str(message.recipient_id))))
    context = message_context(message)
    native = getattr(context, "participants", [])
    if native:
        return (
            "source-participants",
            *(pair if include_reported_pair else ()),
            str(context.source_document_id),
            *sorted(map(str, native)),
        )
    return ("reported-pair", *pair)


@dataclass(frozen=True)
class SessionDecision:
    boundary: str = ""
    continuation: str = ""
    parent: object | None = None
    elapsed_seconds: float | None = None


POLAR_QUESTION = re.compile(
    r"^(?:am|are|can|could|did|do|does|had|has|have|is|may|shall|should|was|were|will|would)\s+\w",
    re.I,
)
POLAR_ANSWER = re.compile(r"^(?:yes|yeah|yep|no|nope)\b", re.I)
SHORT_CONFIRMATION = re.compile(
    r"^(?:yes|yeah|yep|no|nope)(?:[,\s]+(?:i|we|it|they)\s+"
    r"(?:can|could|did|do|will|would|am|are|is|have)(?:n['’]t|\s+not)?)?[.!\s]*$",
    re.I,
)
TIME_ANSWER = re.compile(
    r"^(?:(?:at|around|by|about)\s+)?(?:noon|midnight|today|tomorrow|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"(?:[01]?\d|2[0-3]):[0-5]\d|(?:1[0-2]|0?[1-9])\s*[ap]\.?m|"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d{1,2})"
    r"(?=\s*(?:[.!]|$|o['’]clock\b)))\b",
    re.I,
)


def _clear_answer(question, answer):
    question = question.strip()
    answer = answer.strip()
    if question.count("?") != 1 or not question.endswith("?") or "?" in answer:
        return False
    if POLAR_QUESTION.match(question) and POLAR_ANSWER.match(answer):
        if SHORT_CONFIRMATION.fullmatch(answer):
            return True
        # A longer answer needs an actual reference to the question.
        terms = set(WORD.findall(question.casefold())) - STOPWORDS
        return any(word in terms for word in WORD.findall(answer.casefold()))
    if re.match(r"^(?:when|what time)\s", question, re.I):
        return bool(TIME_ANSWER.match(answer))
    if re.match(r"^where\s", question, re.I):
        return bool(re.match(r"^(?:at|in|on)\s+(?:the|our|my|your)\s+\w", answer, re.I))
    return False


def _delayed_continuation(previous, current, native_parent, gap):
    """A supplied reply pointer takes precedence over any inferred answer."""
    if gap > REPLY_WINDOW:
        return None
    context = message_context(current)
    pointer = getattr(context, "reply_to_id", "")
    if pointer:
        parent_context = message_context(native_parent)
        thread = getattr(context, "native_thread_id", "")
        parent_thread = getattr(parent_context, "native_thread_id", "")
        parent_time = message_event_time(native_parent) if native_parent else None
        if (
            parent_time is None
            or native_parent.case_id != current.case_id
            or participant_stream(native_parent) != participant_stream(current)
            or getattr(parent_context, "source_document_id", None)
            != context.source_document_id
            or getattr(parent_context, "native_message_id", "") != pointer
            or (thread and parent_thread and thread != parent_thread)
        ):
            return None
        elapsed = message_event_time(current) - parent_time
        if timedelta() < elapsed <= REPLY_WINDOW:
            return SessionDecision(
                continuation="The export records a reply to the earlier message.",
                parent=native_parent,
                elapsed_seconds=elapsed.total_seconds(),
            )
    elif (
        current.sender_id != current.recipient_id
        and current.sender_id == previous.recipient_id
        and current.recipient_id == previous.sender_id
        and getattr(previous, "session_wording_complete", True)
        and getattr(current, "session_wording_complete", True)
        and max(len(previous.body), len(current.body)) <= WORDING_LIMIT
        and _clear_answer(previous.body, current.body)
    ):
        return SessionDecision(
            continuation="The other participant appears to answer the question.",
            parent=previous,
            elapsed_seconds=gap.total_seconds(),
        )
    return None


def session_decision(
    previous,
    current,
    *,
    gap_minutes,
    native_parent=None,
    break_before=False,
    join_before=False,
    greeting_boundaries=True,
):
    """Compare one step with its uniquely resolved parent in the open session."""
    current_time = message_event_time(current)
    if current_time is None:
        return SessionDecision(
            boundary="The message time is unknown. Its session is unresolved."
        )
    if (
        previous is None
        or previous.case_id != current.case_id
        or participant_stream(current) != participant_stream(previous)
    ):
        return SessionDecision(
            boundary="First selected message between these participants."
        )
    if break_before:
        return SessionDecision(boundary="Investigator-adjusted boundary")
    previous_time = message_event_time(previous)
    if previous_time is None:
        return SessionDecision(
            boundary="The message time is unknown. Its session is unresolved."
        )
    if join_before:
        return SessionDecision()
    gap = current_time - previous_time
    if gap > timedelta(minutes=gap_minutes):
        if decision := _delayed_continuation(previous, current, native_parent, gap):
            return decision
        return SessionDecision(
            boundary=f"Observed gap exceeds selected {gap_minutes:g}-minute threshold"
        )
    if (
        greeting_boundaries
        and gap >= timedelta(minutes=5)
        and re.match(r"\s*(?:hi|hello|hey|good morning)\b", current.body, re.I)
        and re.search(
            r"\b(?:bye|goodbye|goodnight|talk later)[.!\s]*$", previous.body, re.I
        )
    ):
        return SessionDecision(
            boundary=(
                "Greeting after farewell and at least five observed "
                "minutes; content heuristic"
            )
        )
    return SessionDecision()

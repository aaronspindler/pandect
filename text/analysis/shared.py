"""Source bounds and token rules shared by text and communication analysis."""

import re

from django.core.exceptions import ValidationError

MAX_SOURCES = 500
MAX_TEXT_CHARS = 1_000_000
WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
FUNCTION_WORDS = set(
    (
        "a an the and or but if while because as of at by for "
        "from in into on to with without i me my mine we us our"
        " you your he him his she her it its they them their "
        "this that these those is are was were be been am do "
        "does did have has had not no so than then"
    ).split()
)
STOPWORDS = FUNCTION_WORDS | set(
    "will would can could should may might also just very".split()
)


def bounded_sources(sources):
    sources = list(sources)
    if len(sources) > MAX_SOURCES:
        raise ValidationError("Select at most 500 sources.")
    if len({str(s.id) for s in sources}) != len(sources):
        raise ValidationError("Each input source must be selected once.")
    if sum(len(s.text) for s in sources) > MAX_TEXT_CHARS:
        raise ValidationError(
            "Selected text exceeds the one-million-character analysis limit."
        )
    return sources

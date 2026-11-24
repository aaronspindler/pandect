"""Highlight literal matches without trusting any markup from a source or query."""

import re

from django import template
from django.utils.html import format_html, format_html_join

register = template.Library()


@register.filter
def highlight_passage(value, offsets):
    """Mark one exact character range in the authorized original reader."""
    match = re.fullmatch(r"([0-9]{1,9})-([0-9]{1,9})", offsets or "")
    if not match:
        return value
    start, end = map(int, match.groups())
    if not 0 <= start < end <= len(value):
        return value
    return format_html(
        '{}<mark id="cited-passage" tabindex="-1">{}</mark>{}',
        value[:start],
        value[start:end],
        value[end:],
    )


@register.filter
def highlight_first_match(value, term):
    """Mark one occurrence without expanding large originals into repeated markup."""
    if not value or not term:
        return value
    match = re.search(re.escape(term), value, flags=re.IGNORECASE)
    if match is None:
        return value
    return format_html(
        "{}<mark>{}</mark>{}", value[: match.start()], match[0], value[match.end() :]
    )


@register.filter
def highlight_matches(value, terms):
    if not value or not terms:
        return value
    alternatives = "|".join(
        re.escape(term) for term in sorted(set(terms), key=len, reverse=True) if term
    )
    if not alternatives:
        return value
    parts = re.split(f"({alternatives})", str(value), flags=re.IGNORECASE)
    return format_html_join(
        "",
        "{}",
        (
            (format_html("<mark>{}</mark>", part) if index % 2 else part,)
            for index, part in enumerate(parts)
        ),
    )

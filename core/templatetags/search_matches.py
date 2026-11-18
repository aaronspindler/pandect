"""Highlight literal matches without trusting any markup from a source or query."""

import re

from django import template
from django.utils.html import format_html, format_html_join

register = template.Library()


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

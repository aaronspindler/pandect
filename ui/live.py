"""Render the same authorized page or its explicitly defined live sections."""

from django.shortcuts import render
from django.utils.cache import patch_vary_headers


def render_live(request, template_name, context, *, status=200):
    partial = (
        request.method in {"GET", "HEAD"}
        and request.headers.get("X-Pandect-Live") == "1"
    )
    response = render(
        request, template_name + ("#live" if partial else ""), context, status=status
    )
    patch_vary_headers(response, ("X-Pandect-Live",))
    if partial:
        response["X-Pandect-Live"] = "1"
    return response

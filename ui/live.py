"""Render the same authorized page or its explicitly defined live sections."""

import json

from django.shortcuts import render
from django.utils.cache import patch_vary_headers


def render_live(request, template_name, context, *, status=200, collection=False):
    mode = "collection" if collection else "1"
    partial = (
        request.method in {"GET", "HEAD"}
        and request.headers.get("X-Pandect-Live") == mode
    )
    if partial:
        template_name += "#collection" if collection else "#live"
    response = render(request, template_name, context, status=status)
    patch_vary_headers(response, ("X-Pandect-Live",))
    if partial:
        response["X-Pandect-Live"] = mode
        if collection and "case_activity_versions" in context:
            response["X-Pandect-Activity-Versions"] = json.dumps(
                context["case_activity_versions"]
            )
    return response

"""Common case context for conventional page responses."""

from django.shortcuts import render

from .permissions import case_context


def render_case(request, template, case, *, status=200, **context):
    return render(
        request,
        template,
        case_context(case, request.user) | context,
        status=status,
    )

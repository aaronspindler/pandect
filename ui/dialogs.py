"""Present short workspace forms through their existing authorized views."""

from django.contrib import messages
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils.cache import patch_vary_headers


def render_quick_form(request, template, context, *, status=200):
    quick = request.headers.get("X-Pandect-Form") == "dialog"
    response = render(
        request,
        template + ("#dialog" if quick else ""),
        context | {"quick_dialog": quick},
        status=status,
    )
    if quick:
        response["X-Pandect-Form"] = "dialog"
    patch_vary_headers(response, ("X-Pandect-Form",))
    return response


def wants_record(request):
    return (
        request.method in {"GET", "HEAD"}
        and request.headers.get("X-Pandect-Form") == "record"
    )


def render_record(request, template, context):
    if not context["can_edit"]:
        return HttpResponseForbidden()
    response = render(request, template + "#record", context)
    response["X-Pandect-Form"] = "record"
    return response


def form_saved(request, message, url, **details):
    if request.headers.get("X-Pandect-Form") == "dialog":
        response = JsonResponse({"message": message, "url": url, **details})
        response["X-Pandect-Form"] = "saved"
    else:
        messages.success(request, message)
        response = redirect(url)
    patch_vary_headers(response, ("X-Pandect-Form",))
    return response

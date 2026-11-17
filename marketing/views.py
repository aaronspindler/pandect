from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe

from .catalogue import catalogue_context


@require_safe
def home(request):
    return render(request, "marketing/home.html")


@require_safe
def product(request):
    return redirect("marketing:home", permanent=True)


@require_safe
def evidence(request):
    return render(
        request,
        "marketing/evidence.html",
        catalogue_context(request.GET.get("type", "")),
    )

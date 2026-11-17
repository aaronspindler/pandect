from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe

from .catalogue import capability_sections, evidence_groups


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
        {
            "evidence_groups": evidence_groups(),
            "capability_sections": capability_sections(),
        },
    )

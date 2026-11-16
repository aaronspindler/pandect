from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe

from .catalogue import evidence_groups


@require_safe
def home(request):
    if request.user.is_authenticated:
        return redirect("cases:case_list")
    return render(request, "marketing/home.html")


@require_safe
def evidence(request):
    return render(
        request, "marketing/evidence.html", {"evidence_groups": evidence_groups()}
    )

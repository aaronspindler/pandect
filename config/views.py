from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .access import product_login_required


@require_safe
@product_login_required
def home(request):
    return redirect("cases:case_list")


def permission_denied(request, exception=None):
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse(
            {"code": "permission_denied", "message": "Your case role cannot do this."},
            status=403,
        )
    return render(request, "403.html", status=403)


def not_found(request, exception=None):
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse(
            {"code": "not_found", "message": "This material is not available to you."},
            status=404,
        )
    return render(request, "404.html", status=404)


def server_error(request):
    return render(request, "500.html", status=500)


@require_safe
@never_cache
def health(request):
    """Report database readiness without exposing configuration or case material."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})

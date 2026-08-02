"""Platform-only views for the application shell and runtime checks."""

import logging

from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

logger = logging.getLogger(__name__)


def _runtime_context() -> dict[str, object]:
    return {"checked_at": timezone.now(), "runtime_state": "running"}


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    return render(request, "core/home.html", _runtime_context())


@require_POST
def runtime_status(request: HttpRequest) -> HttpResponse:
    template = (
        "core/partials/runtime_status.html" if _is_htmx(request) else "core/status.html"
    )
    return render(request, template, _runtime_context())


@require_GET
def health(request: HttpRequest) -> HttpResponse:
    """Fast liveness signal with no database or queue dependency."""
    return HttpResponse("ok\n", content_type="text/plain")


@require_GET
def readiness(request: HttpRequest) -> HttpResponse:
    """Report whether the application database accepts a simple query."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        # The underlying exception may contain a machine-specific database path.
        logger.warning("Application database readiness check failed")
        return HttpResponse("not ready\n", status=503, content_type="text/plain")
    return HttpResponse("ready\n", content_type="text/plain")

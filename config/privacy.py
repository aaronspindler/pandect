"""Keep private pages out of caches and search, and redact sensitive URLs."""

import logging
import re
from urllib.parse import unquote

from django.conf import settings
from django.utils.cache import add_never_cache_headers
from django.utils.crypto import salted_hmac
from gunicorn.glogging import Logger

TOKEN_PATH = re.compile(
    r"(/(?:invitations|accounts/(?:confirm-email|password/reset/key))/)[^/?\s\"'<>]+"
)
QUERY = re.compile(r"\?[^\s\"'<>]*")
PRIVATE_PATHS = ("/cases/", "/accounts/", "/invitations/", "/events/", "/admin/")


def redact_url(value):
    sanitized = TOKEN_PATH.sub(r"\1[redacted]", QUERY.sub("", str(value)))
    # A raw request target can encode route segments. Detect those credentials
    # without emitting the decoded target or introducing new log characters.
    if "%" in sanitized:
        decoded = unquote(sanitized)
        if TOKEN_PATH.sub(r"\1[redacted]", decoded) != decoded:
            return "[redacted]"
    return sanitized


def private_session(request):
    """Bind private pages to the authenticated account and this browser session."""
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if (
        request.path_info.startswith(PRIVATE_PATHS)
        and user is not None
        and user.is_authenticated
        and session is not None
        and session.session_key
    ):
        if not hasattr(request, "pandect_private_session"):
            marker = salted_hmac(
                "pandect.private-session",
                f"{user.pk}:{session.session_key}",
                algorithm="sha256",
            )
            if request.path_info.startswith("/admin/"):
                # Admin can show records and controls from more than one case.
                from cases.models import CaseMembership

                permissions = (
                    user.is_staff,
                    user.is_superuser,
                    sorted(user.get_all_permissions()),
                )
                marker.update(repr(permissions).encode())
                roles = (
                    CaseMembership.objects.filter(user=user)
                    .order_by("case_id")
                    .values_list("case_id", "role")
                )
                for case_id, role in roles.iterator():
                    marker.update(f"{case_id}:{role}\n".encode())
            request.pandect_private_session = marker.hexdigest()
        return request.pandect_private_session
    return ""


class PrivatePageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        private_page = request.path_info.startswith(PRIVATE_PATHS)
        if private_page:
            add_never_cache_headers(response)
            # Preserve same-origin form Origin/Referer checks. `no-referrer`
            # makes browsers send Origin: null for ordinary form POSTs.
            response["Referrer-Policy"] = "same-origin"
            response["X-Robots-Tag"] = "noindex"
            if session := private_session(request):
                response["X-Pandect-Session"] = session
        response.setdefault("X-Content-Type-Options", "nosniff")
        tile_source = ""
        match = request.resolver_match
        if (
            response.status_code == 200
            and match
            and match.view_name in ("geo:case_map", "geo:trip_detail")
            and settings.PANDECT_MAP_TILE_ORIGIN
        ):
            tile_source = f" {settings.PANDECT_MAP_TILE_ORIGIN}"
        response.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            f"img-src 'self' data:{tile_source}; font-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        return response


class RedactPrivateURLs(logging.Filter):
    def filter(self, record):
        record.msg = redact_url(record.getMessage())
        record.args = ()
        if 400 <= getattr(record, "status_code", 0) < 500 or record.name.startswith(
            ("django.tasks", "django_tasks_db")
        ):
            # Task exceptions can contain untrusted evidence or secrets. Retain
            # diagnostics in the private task store, not routine worker logs.
            # Expected permission/validation failures also need no traceback.
            record.exc_info = None
            record.exc_text = None
        elif record.name == "gunicorn.error" and record.exc_info:
            # Protocol exceptions can repeat the request target in the traceback.
            record.exc_text = redact_url(
                logging.Formatter().formatException(record.exc_info)
            )
        return True


class PrivateAccessLogger(Logger):
    _redact_errors = RedactPrivateURLs()

    def setup(self, cfg):
        super().setup(cfg)
        self.error_log.addFilter(self._redact_errors)

    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        for key in (
            "r",
            "U",
            "f",
            "{raw_uri}e",
            "{path_info}e",
            "{http_referer}e",
            "{referer}i",
            "{location}o",
        ):
            if key in atoms:
                atoms[key] = redact_url(atoms[key])
        atoms["q"] = ""
        if "{query_string}e" in atoms:
            atoms["{query_string}e"] = ""
        return atoms

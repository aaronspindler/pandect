"""Keep private pages out of caches and sensitive URLs out of routine logs."""

import logging
import re
from urllib.parse import unquote

from django.conf import settings
from django.utils.cache import add_never_cache_headers
from gunicorn.glogging import Logger

TOKEN_PATH = re.compile(
    r"(/invitations/|/accounts/(?:confirm-email/|password/reset/key/))[^/?\s\"'<>]+"
)
QUERY = re.compile(r"\?[^\s\"'<>]*")


def redact_url(value):
    sanitized = TOKEN_PATH.sub(r"\1[redacted]", QUERY.sub("", str(value)))
    if "%" in sanitized:
        # Gunicorn can log the raw request target even when routing used a
        # decoded path. Detect encoded route segments without logging decoded data.
        decoded = unquote(sanitized)
        if TOKEN_PATH.sub(r"\1[redacted]", decoded) != decoded:
            return "[redacted]"
    return sanitized


class PrivatePageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(("/cases/", "/accounts/", "/invitations/")):
            add_never_cache_headers(response)
            # Preserve same-origin form Origin/Referer checks. `no-referrer`
            # makes browsers send Origin: null for ordinary form POSTs.
            response["Referrer-Policy"] = "same-origin"
        response.setdefault("X-Content-Type-Options", "nosniff")
        tile_source = ""
        match = request.resolver_match
        if (
            response.status_code == 200
            and match
            and match.view_name == "cases:case_map"
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
        return True


class PrivateAccessLogger(Logger):
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

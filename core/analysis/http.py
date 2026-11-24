"""Shared redirect policy for transports carrying case material or credentials."""

from http.client import HTTPException
from urllib.request import HTTPRedirectHandler

from core.analysis.inference_payloads import decode_json


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def provider_failure_status(error):
    """Classify a provider failure without retaining its response content."""
    raw = b""
    try:
        if error.code == 429:
            raw = error.read(8192)
    except OSError, HTTPException, ValueError:
        pass
    finally:
        error.close()
    if error.code == 429:
        try:
            value = decode_json(raw)
        except ValueError, RecursionError:
            value = None
        detail = value.get("error") if isinstance(value, dict) else None
        if isinstance(detail, dict) and detail.get("code") == "insufficient_quota":
            return "quota", len(raw)
        return "rate_limit", len(raw)
    if error.code in (401, 403):
        return "authentication", 0
    if error.code == 402:
        return "quota", 0
    return "provider_failure", 0

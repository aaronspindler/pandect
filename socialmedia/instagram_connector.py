"""Synthetic Instagram fixture transport and legacy connection revocation."""

import json
import re
from datetime import UTC, datetime
from secrets import token_urlsafe
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID

from django.conf import settings
from django.core import signing

from core.analysis.http import NoRedirect

PILOT_TARGET = "demo.account"
PILOT_TARGET_ID = "synthetic:demo.account"
VERSION = "synthetic-browser-v1"
MAX_RESPONSE = 4 * 1024 * 1024
SYNTHETIC_SESSION_SECONDS = 30 * 86400


class ConnectorError(Exception):
    def __init__(self, code, *, retry_after=None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


def allowed_target(source, handle, interval_hours):
    return (
        source == "synthetic"
        and handle == PILOT_TARGET
        and interval_hours in (1, 6, 12, 24)
    )


def scheduling_available():
    return bool(settings.PANDECT_INSTAGRAM_PILOT_URL) and (
        len(settings.PANDECT_INSTAGRAM_PILOT_KEY) >= 32
    )


def _signer(purpose):
    if not scheduling_available():
        raise ConnectorError("not_configured")
    return signing.TimestampSigner(
        key=settings.PANDECT_INSTAGRAM_PILOT_KEY,
        salt="instagram-synthetic-" + purpose,
        fallback_keys=[],
    )


def connection_expires_at(session):
    if not session or not scheduling_available():
        return None
    signer = _signer("observer")
    try:
        signer.unsign_object(session, max_age=SYNTHETIC_SESSION_SECONDS)
        timestamp = session.rsplit(signer.sep, 2)[1]
        return datetime.fromtimestamp(
            signing.b62_decode(timestamp) + SYNTHETIC_SESSION_SECONDS, tz=UTC
        )
    except signing.BadSignature, ValueError, TypeError, OverflowError, OSError:
        return None


def start_pairing(user_id):
    token = _signer("pair").sign_object([str(UUID(str(user_id))), token_urlsafe(16)])
    return {"id": token, "url": ""}


def finish_pairing(user_id, pair_id):
    try:
        owner, nonce = _signer("pair").unsign_object(pair_id, max_age=600)
        if owner != str(UUID(str(user_id))):
            raise ConnectorError("pairing_expired")
    except signing.BadSignature, ValueError, TypeError:
        raise ConnectorError("pairing_expired") from None
    observer = "pilot." + UUID(owner).hex[:20]
    return {
        "observer": observer,
        "observer_id": observer,
        "session": _signer("observer").sign_object([observer, nonce]),
    }


def collect(session, target):
    if target != PILOT_TARGET:
        raise ConnectorError("access_lost")
    try:
        observer, _ = _signer("observer").unsign_object(
            session, max_age=SYNTHETIC_SESSION_SECONDS
        )
    except signing.BadSignature, ValueError, TypeError:
        raise ConnectorError("challenge") from None
    if not re.fullmatch(r"pilot\.[0-9a-f]{20}", observer):
        raise ConnectorError("invalid_response")
    request = Request(
        settings.PANDECT_INSTAGRAM_PILOT_URL.rstrip("/") + "/collect",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Only the fixed synthetic source can be requested. Even the pilot's observer
    # name and session ticket stay in Pandect.
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            request, timeout=45
        ) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ConnectorError("invalid_response")
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("collector_version") != VERSION
            or data.get("target") != PILOT_TARGET
            or not isinstance(data.get("followers"), dict)
            or not isinstance(data.get("following"), dict)
        ):
            raise ConnectorError("invalid_response")
    except HTTPError as exc:
        code = "cooldown" if exc.code == 429 else "unavailable"
        exc.close()
        raise ConnectorError(
            code, retry_after=60 if code == "cooldown" else None
        ) from None
    except URLError, OSError:
        raise ConnectorError("unavailable") from None
    except ValueError, RecursionError:
        raise ConnectorError("invalid_response") from None
    return {
        **data,
        "observer": observer,
        "observer_id": observer,
        "target_id": PILOT_TARGET_ID,
    }


def revoke_live(session):
    """Disconnect an old signed desktop binding from its original loopback host."""
    config = settings.PANDECT_INSTAGRAM_LEGACY_REVOCATION
    if not config:
        raise ConnectorError("not_configured")
    url, user, case = (config[key] for key in ("url", "user_id", "case_id"))
    try:
        token = signing.Signer(
            key=config["token"],
            fallback_keys=[],
            salt=f"instagram-session:{url}:{user}:{case}",
        ).unsign(session)
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
            raise ConnectorError("disconnected")
    except signing.BadSignature, TypeError:
        raise ConnectorError("disconnected") from None
    request = Request(
        url + "/disconnect",
        data=json.dumps({"session": token}).encode(),
        headers={
            "Authorization": "Bearer " + config["token"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=25):
            pass
    except HTTPError as error:
        error.close()
        raise ConnectorError("unavailable") from None
    except OSError:
        raise ConnectorError("unavailable") from None

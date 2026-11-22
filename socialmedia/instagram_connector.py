"""Offline fixture and explicitly configured loopback Instagram pilot transports."""

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
from socialmedia.instagram_rosters import LIVE_VERSION

PILOT_TARGET = "demo.account"
PILOT_TARGET_ID = "synthetic:demo.account"
VERSION = "synthetic-browser-v1"
LIVE_COLLECTION_SECONDS = 600
MAX_RESPONSE = 4 * 1024 * 1024
SYNTHETIC_SESSION_SECONDS = 30 * 86400


class ConnectorError(Exception):
    def __init__(self, code, *, retry_after=None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


def current_source():
    return "instagram" if settings.PANDECT_INSTAGRAM_LIVE else "synthetic"


def permits(user_id, case_id=None):
    if current_source() == "synthetic":
        return True
    config = settings.PANDECT_INSTAGRAM_LIVE
    return str(user_id) == config["user_id"] and (
        case_id is None or str(case_id) == config["case_id"]
    )


def allowed_targets():
    if current_source() == "instagram":
        return settings.PANDECT_INSTAGRAM_LIVE["targets"]
    return (PILOT_TARGET,)


def allowed_target(source, handle, interval_hours):
    minimum = settings.PANDECT_INSTAGRAM_LIVE.get("min_hours", 1)
    return (
        source == current_source()
        and handle in allowed_targets()
        and interval_hours in (1, 6, 12, 24)
        and interval_hours >= minimum
    )


def available():
    if current_source() == "instagram":
        return True
    return bool(settings.PANDECT_INSTAGRAM_PILOT_URL) and (
        len(settings.PANDECT_INSTAGRAM_PILOT_KEY) >= 32
    )


def scheduling_available():
    # The signed-in website experiment has no qualified scheduled source.
    return current_source() == "synthetic" and available()


def _signer(purpose):
    if not available():
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
    if current_source() == "instagram":
        if not permits(user_id):
            raise ConnectorError("access_lost")
        data = _live_request("/pair/start", {"user_id": str(user_id)})
        return {"id": _live_signer("id").sign(_binding(data, "id")), "url": ""}
    token = _signer("pair").sign_object([str(UUID(str(user_id))), token_urlsafe(16)])
    return {"id": token, "url": ""}


def finish_pairing(user_id, pair_id):
    if current_source() == "instagram":
        if not permits(user_id):
            raise ConnectorError("access_lost")
        data = _live_request(
            "/pair/finish", {"user_id": str(user_id), "id": _open_live("id", pair_id)}
        )
        observer = data.get("observer")
        expected = settings.PANDECT_INSTAGRAM_LIVE["observer"]
        if (
            not isinstance(observer, str)
            or not re.fullmatch(r"[a-z0-9._]{1,30}", observer)
            or (expected and observer != expected)
        ):
            raise ConnectorError("access_lost")
        return {
            "observer": observer,
            "source": "instagram",
            "session": _live_signer("session").sign(_binding(data, "session")),
        }
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
    if current_source() == "instagram":
        if target not in allowed_targets():
            raise ConnectorError("access_lost")
        data = _live_request(
            "/collect", {"session": _open_live("session", session), "target": target}
        )
        if (
            data.get("source") != "instagram"
            or data.get("collector_version") != LIVE_VERSION
            or data.get("target") != target
            or (
                settings.PANDECT_INSTAGRAM_LIVE["observer"]
                and data.get("observer") != settings.PANDECT_INSTAGRAM_LIVE["observer"]
            )
        ):
            raise ConnectorError("invalid_response")
        return data
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


def _live_signer(purpose):
    config = settings.PANDECT_INSTAGRAM_LIVE
    if not config:
        raise ConnectorError("not_configured")
    return signing.Signer(
        key=config["token"],
        fallback_keys=[],
        salt=f"instagram-{purpose}:{config['url']}:{config['user_id']}:{config['case_id']}",
    )


def _open_live(purpose, value):
    try:
        return _live_signer(purpose).unsign(value)
    except signing.BadSignature, TypeError:
        raise ConnectorError(
            "pairing_expired" if purpose == "id" else "disconnected"
        ) from None


def _binding(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", value):
        raise ConnectorError("invalid_response")
    return value


def _live_request(path, data):
    config = settings.PANDECT_INSTAGRAM_LIVE
    if not config:
        raise ConnectorError("not_configured")
    request = Request(
        config["url"] + path,
        data=json.dumps(data).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + config["token"],
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
            request, timeout=LIVE_COLLECTION_SECONDS + 35 if path == "/collect" else 25
        ) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ConnectorError("invalid_response")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ConnectorError("invalid_response")
        return value
    except HTTPError as exc:
        code = {
            401: "disconnected",
            403: "access_lost",
            404: "pairing_expired",
            409: "challenge",
            425: "pairing_pending",
            429: "cooldown",
        }.get(exc.code, "unavailable")
        delay = str(exc.headers.get("Retry-After", ""))
        exc.close()
        raise ConnectorError(
            code, retry_after=int(delay) if re.fullmatch(r"[0-9]{1,7}", delay) else None
        ) from None
    except URLError, OSError:
        raise ConnectorError("unavailable") from None
    except ValueError, RecursionError:
        raise ConnectorError("invalid_response") from None


def revoke_live(session):
    _live_request("/disconnect", {"session": _open_live("session", session)})


def cancel_pairing(user_id, token):
    if current_source() == "instagram" and permits(user_id):
        _live_request(
            "/pair/cancel", {"user_id": str(user_id), "id": _open_live("id", token)}
        )

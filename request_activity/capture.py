"""Capture a small request snapshot and persist it outside the ASGI worker loop."""

import hmac
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import IPv6Address, ip_address, ip_network
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from . import spool

PUBLIC_PAGES = {
    "/": "home",
    "/evidence/": "evidence",
    "/investigations/": "investigations",
    "/investigations/incidents/": "investigation-incidents",
    "/investigations/osint/": "investigation-osint",
    "/investigations/communications/": "investigation-communications",
    "/investigations/vehicle-location/": "investigation-vehicle-location",
    "/roadmap/": "roadmap",
    "/trust/": "trust",
}
PRIVATE_AREAS = {
    "accounts",
    "admin",
    "cases",
    "events",
    "invitations",
    "static",
}
METHODS = {
    "CONNECT",
    "DELETE",
    "GET",
    "HEAD",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
    "TRACE",
}
HEADER_LIMITS = {
    "host": 260,
    "cf-connecting-ip": 45,
    "user-agent": 512,
    "accept-language": 128,
}
QUEUE_SIZE = 512
BATCH_SIZE = 32
HEARTBEAT_SECONDS = 5


def _headers(pairs):
    """Select bounded singleton values from Gunicorn's original header pairs."""
    selected = {}
    for name, value in pairs:
        name = name.lower()
        limit = HEADER_LIMITS.get(name)
        if limit:
            selected[name] = (
                value.strip() if name not in selected and len(value) <= limit else ""
            )
    return selected


def _ip(value):
    # Do not store a scoped address or parse a list of forwarded values.
    if not value or "%" in value or len(value) > 45:
        return None
    try:
        address = ip_address(value)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _client_ip(peer_value, forwarded_value, trusted_proxies):
    peer = _ip(peer_value)
    if peer is None or not any(peer in network for network in trusted_proxies):
        return None
    forwarded = _ip(forwarded_value)
    return str(forwarded) if forwarded is not None else None


def _route_label(path):
    if path in PUBLIC_PAGES:
        return PUBLIC_PAGES[path]
    if path in ("/health/", "/events/health/"):
        return "health"
    if path in ("/robots.txt", "/sitemap.xml"):
        return "site-metadata"
    area = path.split("/", 2)[1] if path.startswith("/") else ""
    return area if area in PRIVATE_AREAS else "other"


def _html_response(response):
    return any(
        name.lower() == "content-type"
        and value.split(";", 1)[0].strip().lower() == "text/html"
        for name, value in response.headers
    )


@dataclass(frozen=True, slots=True)
class Snapshot:
    occurred_at: datetime
    method: str
    status: int | None
    route: str
    ip: str | None
    user_agent: str
    accept_language: str
    public_html: bool

    def event(self, key, version):
        user_agent = " ".join(self.user_agent.split()).casefold()
        language = " ".join(self.accept_language.split()).casefold()
        fingerprint = None
        if user_agent:
            fields = json.dumps([user_agent, language], separators=(",", ":")).encode()
            digest = hmac.digest(key, fields, "sha256").hex()
            fingerprint = f"v{version}:{digest}"
        return {
            "event_id": str(uuid4()),
            "occurred_at": self.occurred_at.isoformat(),
            "method": self.method,
            "status": self.status,
            "route": self.route,
            "ip": self.ip,
            "fingerprint": fingerprint,
            "public_html": self.public_html,
        }


class RequestCapture:
    def __init__(
        self, *, hosts, spool_path, trusted_proxies, key, version, logger, now=None
    ):
        self.hosts = hosts
        self.spool_path = spool_path
        self.trusted_proxies = trusted_proxies
        self.key = key
        self.version = version
        self.logger = logger
        self.now = now or (lambda: datetime.now(UTC))
        self.queue = Queue(maxsize=QUEUE_SIZE)
        self.stop_requested = Event()
        self.dropped = 0
        self.last_gap_at = None
        self.last_progress = monotonic()
        self.spool_unavailable = False
        self.writer = Thread(
            target=self._write, name="request-activity-writer", daemon=True
        )
        self.watchdog = Thread(
            target=self._watch, name="request-activity-watchdog", daemon=True
        )

    @classmethod
    def from_environment(cls, logger):
        origin = urlsplit(os.environ.get("PANDECT_PUBLIC_ORIGIN", ""))
        hostname = (origin.hostname or "").removesuffix(".")
        if (
            origin.scheme not in {"http", "https"}
            or not hostname
            or origin.username is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Invalid request activity origin")
        port = origin.port or (443 if origin.scheme == "https" else 80)
        aliases = {f"[{hostname}]"} if ":" in hostname else {hostname, f"{hostname}."}
        hosts = aliases | {f"{alias}:{port}" for alias in aliases}
        path = Path(os.environ.get("PANDECT_REQUEST_ACTIVITY_SPOOL", ""))
        key = os.environ.get("PANDECT_REQUEST_ACTIVITY_KEY", "").encode()
        version = int(os.environ.get("PANDECT_REQUEST_ACTIVITY_KEY_VERSION", "1"))
        networks = tuple(
            ip_network(network.strip())
            for network in os.environ.get(
                "PANDECT_REQUEST_ACTIVITY_TRUSTED_PROXIES", ""
            ).split(",")
            if network.strip()
        )
        if (
            not path.is_absolute()
            or len(key) < 32
            or not 1 <= version <= 9999
            or any(network.prefixlen == 0 for network in networks)
        ):
            raise ValueError("Invalid request activity settings")
        return cls(
            hosts=hosts,
            spool_path=path,
            trusted_proxies=networks,
            key=key,
            version=version,
            logger=logger,
        )

    def start(self):
        self.watchdog.start()
        try:
            self.writer.start()
        except RuntimeError:
            self.stop_requested.set()
            raise

    def stop(self):
        self.stop_requested.set()
        self.writer.join(timeout=3)
        if self.writer.is_alive():
            self.logger.warning(
                "Request activity capture stopped before its queue drained."
            )

    def enqueue(self, response, request, environ, request_time):
        """Bounded memory handoff; Gunicorn calls this inline on the ASGI worker."""
        try:
            headers = _headers(request.headers)
            if headers.get("host", "").lower() not in self.hosts:
                return
            method = environ["REQUEST_METHOD"]
            path = environ["PATH_INFO"]
            status = response.status
            if not isinstance(status, int) or not 100 <= status <= 599:
                status = None
            elapsed = timedelta(
                seconds=request_time.seconds, microseconds=request_time.microseconds
            )
            self.queue.put_nowait(
                Snapshot(
                    occurred_at=self.now() - elapsed,
                    method=method if method in METHODS else "OTHER",
                    status=status,
                    route=_route_label(path),
                    ip=_client_ip(
                        environ.get("REMOTE_ADDR"),
                        headers.get("cf-connecting-ip"),
                        self.trusted_proxies,
                    ),
                    user_agent=headers.get("user-agent", ""),
                    accept_language=headers.get("accept-language", ""),
                    public_html=(
                        method == "GET"
                        and path in PUBLIC_PAGES
                        and status is not None
                        and 200 <= status < 300
                        and _html_response(response)
                    ),
                )
            )
        except AttributeError, Full, KeyError, OverflowError, TypeError, ValueError:
            self.last_gap_at = self.now().isoformat()
            self.dropped += 1

    def _next_batch(self):
        batch = []
        try:
            batch.append(self.queue.get(timeout=1))
        except Empty:
            return batch
        for _ in range(BATCH_SIZE - 1):
            try:
                batch.append(self.queue.get_nowait())
            except Empty:
                break
        return [snapshot.event(self.key, self.version) for snapshot in batch]

    def _write(self):
        batch = []
        next_heartbeat = 0
        last_persisted_gap_at = None
        while (
            not self.stop_requested.is_set()
            or batch
            or not self.queue.empty()
            or self.last_gap_at != last_persisted_gap_at
        ):
            try:
                with spool.open_writer(self.spool_path) as db:
                    while not self.stop_requested.is_set() or (
                        batch or not self.queue.empty()
                    ):
                        if not batch:
                            batch = self._next_batch()
                        if batch or monotonic() >= next_heartbeat:
                            gap_at = self.last_gap_at
                            spool.write_batch(db, batch, self.now().isoformat(), gap_at)
                            batch = []
                            last_persisted_gap_at = gap_at
                            next_heartbeat = monotonic() + HEARTBEAT_SECONDS
                            self.spool_unavailable = False
                        self.last_progress = monotonic()
                    gap_at = self.last_gap_at
                    if gap_at != last_persisted_gap_at:
                        spool.write_batch(db, [], self.now().isoformat(), gap_at)
                        last_persisted_gap_at = gap_at
            except OSError, sqlite3.Error:
                self.spool_unavailable = True
                if self.stop_requested.wait(1):
                    if (
                        batch
                        or not self.queue.empty()
                        or self.last_gap_at != last_persisted_gap_at
                    ):
                        self.logger.warning(
                            "Request activity capture stopped with unpersisted records."
                        )
                    return

    def _watch(self):
        unhealthy = False
        reported_dropped = 0
        while not self.stop_requested.wait(HEARTBEAT_SECONDS):
            stalled = monotonic() - self.last_progress >= 3 * HEARTBEAT_SECONDS
            problem = self.spool_unavailable or stalled
            dropped = self.dropped
            if (problem and not unhealthy) or dropped != reported_dropped:
                self.logger.warning(
                    "Request activity capture is degraded. Records dropped since "
                    "worker start: %d. Spool unavailable or stalled: %s.",
                    dropped,
                    problem,
                )
            elif not problem and unhealthy:
                self.logger.info("Request activity spool writes have recovered.")
            unhealthy = problem
            reported_dropped = dropped

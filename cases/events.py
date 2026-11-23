"""Same-origin, case-authorized change signals backed by committed revisions."""

import asyncio
import json
from collections import defaultdict
from contextlib import suppress
from io import BytesIO

import psycopg
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import DisallowedHost
from django.core.handlers.asgi import ASGIRequest
from django.db import DatabaseError, close_old_connections, connections
from django.urls import Resolver404, resolve

from cases.permissions import accessible_cases

HEARTBEAT_SECONDS = 15
MAX_STREAMS = 64
PRIVATE_HEADERS = [
    (b"cache-control", b"private, no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
]


def can_subscribe(request, case_id):
    # A fresh session object also catches logout, expiry or a changed password
    # on an already open stream. Never retain an ORM connection while waiting.
    close_old_connections()
    try:
        SessionMiddleware(lambda request: None).process_request(request)
        user = get_user(request)
        return (
            user.is_authenticated
            and user.is_active
            and EmailAddress.objects.filter(user=user, verified=True).exists()
            and accessible_cases(user).filter(pk=case_id).exists()
        )
    finally:
        close_old_connections()


async def respond(send, status, body=b"", headers=()):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [*PRIVATE_HEADERS, *headers],
        }
    )
    await send({"type": "http.response.body", "body": body})


class CaseEvents:
    def __init__(self):
        self.subscribers = defaultdict(set)
        self.listening = False
        self.slots = asyncio.Semaphore(MAX_STREAMS)
        self.authorizers = asyncio.Semaphore(8)

    async def authorized(self, request, case_id):
        async with self.authorizers:
            return await asyncio.to_thread(can_subscribe, request, case_id)

    async def listen(self):
        params = connections["default"].get_connection_params()
        # Use psycopg's native async cursor, independent of Django ORM requests.
        params.pop("cursor_factory", None)
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    **params, autocommit=True, application_name="pandect_case_events"
                ) as connection:
                    await connection.execute("LISTEN pandect_case_activity")
                    self.listening = True
                    async for change in connection.notifies():
                        for wake in self.subscribers.get(change.payload, ()):
                            wake.set()
            except psycopg.Error:
                pass
            finally:
                self.listening = False
                # Reconnecting browsers take another snapshot after LISTEN
                # succeeds, recovering every transaction committed during a gap.
                for group in self.subscribers.values():
                    for wake in group:
                        wake.set()
            await asyncio.sleep(2)

    async def lifespan(self, receive, send):
        await receive()
        listener = asyncio.create_task(self.listen())
        try:
            await send({"type": "lifespan.startup.complete"})
            await receive()
        finally:
            listener.cancel()
            with suppress(asyncio.CancelledError):
                await listener
        await send({"type": "lifespan.shutdown.complete"})

    async def stream(self, request, case_id, receive, send):
        wake = asyncio.Event()
        group = self.subscribers[str(case_id)]
        group.add(wake)

        async def disconnect():
            while (await receive())["type"] != "http.disconnect":
                pass
            wake.set()

        disconnected = asyncio.create_task(disconnect())
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        *PRIVATE_HEADERS,
                        (b"content-type", b"text/event-stream"),
                        (b"x-accel-buffering", b"no"),
                    ],
                }
            )
            body = b": connected\n\n"
            while self.listening and not disconnected.done():
                async with asyncio.timeout(10):
                    await send(
                        {"type": "http.response.body", "body": body, "more_body": True}
                    )
                try:
                    await asyncio.wait_for(wake.wait(), HEARTBEAT_SECONDS)
                    wake.clear()
                    body = b"event: change\ndata: {}\n\n"
                except TimeoutError:
                    body = b": heartbeat\n\n"
                if disconnected.done() or not self.listening:
                    break
                try:
                    allowed = await self.authorized(request, case_id)
                except DatabaseError:
                    # The browser reconciles from its authorized snapshot
                    # when the database is reachable again.
                    break
                if not allowed:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"event: access-lost\ndata: {}\n\n",
                        }
                    )
                    return
            if not disconnected.done():
                await send({"type": "http.response.body", "body": b""})
        finally:
            disconnected.cancel()
            with suppress(asyncio.CancelledError):
                await disconnected
            group.discard(wake)
            if not group:
                del self.subscribers[str(case_id)]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.lifespan(receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close"})
            return
        request = ASGIRequest(scope, BytesIO())
        try:
            host = request.get_host()
        except DisallowedHost:
            return await respond(send, 400)
        if request.method != "GET":
            return await respond(send, 405, headers=[(b"allow", b"GET")])
        if request.path_info == "/events/health/":
            status = "ok" if self.listening else "unavailable"
            body = json.dumps(
                {
                    "status": status,
                    "version": settings.APP_VERSION,
                    "commit": settings.APP_COMMIT_SHA,
                }
            ).encode()
            return await respond(
                send,
                200 if self.listening else 503,
                body,
                [(b"content-type", b"application/json")],
            )
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return await respond(send, 404)
        if match.view_name != "case_events":
            return await respond(send, 404)
        origin = request.headers.get("Origin")
        if request.headers.get("Sec-Fetch-Site") not in (
            None,
            "same-origin",
            "none",
        ) or (origin and origin != f"{request.scheme}://{host}"):
            return await respond(send, 403)
        case_id = match.kwargs["case_id"]
        if self.slots.locked() or not self.listening:
            return await respond(send, 503)
        async with self.slots:
            try:
                allowed = await self.authorized(request, case_id)
            except DatabaseError:
                return await respond(send, 503)
            if not allowed:
                return await respond(send, 404)
            await self.stream(request, case_id, receive, send)


application = CaseEvents()

"""Bound ASGI request bodies after the route and upload permissions are known."""

import asyncio
import tempfile

from django.conf import settings
from django.core.handlers.asgi import ASGIHandler


class RequestBody:
    def __init__(self, receive):
        self.receive = receive
        self.watch_disconnect = asyncio.Event()
        # Only authorized uploads roll over to disk before multipart parsing.
        self.file = tempfile.SpooledTemporaryFile(mode="w+b")

    async def __call__(self):
        # Django's concurrent disconnect watcher must not take the next body chunk.
        await self.watch_disconnect.wait()
        while True:
            message = await self.receive()
            if message["type"] == "http.disconnect":
                return message

    def write_to_disk(self, chunk):
        self.file.rollover()
        self.file.write(chunk)

    async def load(self, limit, *, allow_files):
        total = 0
        while True:
            message = await self.receive()
            if message["type"] == "http.disconnect":
                raise ConnectionAbortedError
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                return None
            if chunk:
                if allow_files and total > settings.FILE_UPLOAD_MAX_MEMORY_SIZE:
                    await asyncio.to_thread(self.write_to_disk, chunk)
                else:
                    self.file.write(chunk)
            if not message.get("more_body", False):
                self.watch_disconnect.set()
                return total


class PandectASGIHandler(ASGIHandler):
    async def handle(self, scope, receive, send):
        body = RequestBody(receive)
        await super().handle({**scope, "pandect.body": body}, body, send)

    async def read_body(self, receive):
        # ProductUploadAccessMiddleware fills this file after authentication and
        # URL resolution. An early middleware response or 404 needs no body.
        return receive.file

    async def run_get_response(self, request):
        response = await super().run_get_response(request)
        if response.streaming:
            # WhiteNoise and other early responses skip view middleware. Once
            # a stream is ready, discard any unused input and watch for closure.
            request.scope["pandect.body"].watch_disconnect.set()
        return response

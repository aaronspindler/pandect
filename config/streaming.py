"""Keep Django's synchronous file and export producers bounded under ASGI."""

from asgiref.sync import sync_to_async
from django.core.handlers.asgi import ASGIHandler, ASGIRequest
from django.http import FileResponse
from django.utils.deprecation import MiddlewareMixin


def _blocks(parts):
    """Coalesce small JSON tokens and split large fragments for bounded writes."""
    block = bytearray()
    for part in parts:
        view = memoryview(part)
        while view:
            take = min(len(view), ASGIHandler.chunk_size - len(block))
            block.extend(view[:take])
            view = view[take:]
            if len(block) == ASGIHandler.chunk_size:
                yield bytes(block)
                block.clear()
    if block:
        yield bytes(block)


async def _async_body(parts, head):
    if head:
        return
    # A disconnect queues Django's response.close() on this same request thread,
    # so the file cannot be closed in the middle of an outstanding disk read.
    next_block = sync_to_async(next, thread_sensitive=True)
    while (block := await next_block(parts, None)) is not None:
        yield block


class AsyncStreamingMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        if (
            isinstance(request, ASGIRequest)
            and response.streaming
            and not response.is_async
        ):
            # Keep Django's existing file closers, content headers, and range wrapper.
            parts = response.streaming_content
            if not isinstance(response, FileResponse):
                parts = _blocks(parts)
            response.streaming_content = _async_body(parts, request.method == "HEAD")
        return response

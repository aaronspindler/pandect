"""Serve Django HTTP and case change streams from one Gunicorn ASGI worker."""

import os

import django
from django.core.handlers.asgi import get_script_prefix

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup(set_prefix=False)

from cases.events import application as case_events  # noqa: E402
from config.http_body import PandectASGIHandler  # noqa: E402

http_application = PandectASGIHandler()


async def application(scope, receive, send):
    prefix = get_script_prefix(scope).rstrip("/")
    if scope["type"] != "http" or scope["path"].startswith(
        ("/events/", f"{prefix}/events/")
    ):
        await case_events(scope, receive, send)
    else:
        await http_application(scope, receive, send)

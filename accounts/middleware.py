"""Use the account preference or this browser's timezone for each request."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


class TimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        name = (
            request.user.timezone if request.user.is_authenticated else ""
        ) or request.COOKIES.get("pandect_timezone", "")
        try:
            zone = ZoneInfo(name) if name and len(name) <= 100 else None
        except ZoneInfoNotFoundError, ValueError:
            zone = None
        # Restore the previous context even when rendering raises an exception.
        with timezone.override(zone):
            return self.get_response(request)

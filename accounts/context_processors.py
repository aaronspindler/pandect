"""Account registration availability for shared navigation."""

from django.conf import settings


def registration(request):
    return {"allow_signups": settings.PANDECT_ALLOW_SIGNUPS}

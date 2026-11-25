"""Account registration availability for shared navigation."""

from django.conf import settings

from config.privacy import private_session


def registration(request):
    return {
        "allow_signups": settings.PANDECT_ALLOW_SIGNUPS,
        "private_session": private_session(request),
    }

"""Inline playback only after bounded inspection of the selected recording."""

from django import template
from django.urls import reverse

from audio.previews import inspect_recording, playback_message
from core.analysis.errors import AnalysisError

register = template.Library()


@register.inclusion_tag("audio/player.html", takes_context=True)
def audio_reader(context, item, attachment=None):
    kind = context.get("kind", "audio")
    result = {"label": attachment.original_filename if attachment else item.title}
    try:
        _, _, parsed = inspect_recording(item, kind=kind, attachment=attachment)
    except AnalysisError as error:
        return result | {"error": playback_message(error)}
    kwargs = {"case_id": item.case_id, "evidence_id": item.pk}
    route = "core:evidence:audio_play"
    if attachment:
        route = "phone:attachment_play"
        kwargs |= {"kind": kind, "attachment_id": attachment.pk}
    return result | {"play_url": reverse(route, kwargs=kwargs), "recording": parsed}

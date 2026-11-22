from zoneinfo import ZoneInfo

from django import template
from django.utils.formats import date_format
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def timeline_time(stamp, display_timezone, day=None):
    # Django's timezone template filter drops fold on repeated local times.
    local = stamp.astimezone(ZoneInfo(display_timezone))
    label = local.strftime("%H:%M:%S")
    if local.microsecond:
        label += local.strftime(".%f").rstrip("0")
    label += " " + local.tzname()
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        label += local.strftime(" (%z)")
    if local.date() != day:
        label = date_format(local, "d M Y") + " " + label
    return format_html('<time datetime="{}">{}</time>', local.isoformat(), label)

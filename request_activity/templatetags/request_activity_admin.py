from django import template

from request_activity.views import can_view_request_activity

register = template.Library()
register.simple_tag(can_view_request_activity)

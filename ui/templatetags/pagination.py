"""Dynamic pagination keys for Django's querystring tag."""

from django import template

register = template.Library()


@register.filter
def page_parameter(number, key):
    return {key: number}

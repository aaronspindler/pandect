from django import template

from socialmedia import instagram_connector
from socialmedia.models import InstagramConnection

register = template.Library()


@register.simple_tag
def show_instagram_connection(user):
    if instagram_connector.available() and instagram_connector.permits(user.pk):
        return True
    return InstagramConnection.objects.filter(user=user).exists()

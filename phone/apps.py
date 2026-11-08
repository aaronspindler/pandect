from django.apps import AppConfig


class PhoneConfig(AppConfig):
    name = "phone"

    def ready(self):
        from . import signals  # noqa: F401

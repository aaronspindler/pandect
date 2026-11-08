from django.apps import AppConfig


class AudioConfig(AppConfig):
    name = "audio"

    def ready(self):
        from . import signals  # noqa: F401

from django.apps import AppConfig


class ImageConfig(AppConfig):
    name = "image"

    def ready(self):
        from . import signals  # noqa: F401

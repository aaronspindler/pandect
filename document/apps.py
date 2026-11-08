from django.apps import AppConfig


class DocumentConfig(AppConfig):
    name = "document"

    def ready(self):
        from . import signals  # noqa: F401

from django.apps import AppConfig


class EvidenceConfig(AppConfig):
    name = "evidence"

    def ready(self):
        from . import signals  # noqa: F401

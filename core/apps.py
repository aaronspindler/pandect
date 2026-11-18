from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        from . import signals  # noqa: F401
        from .case_search import index_hooks  # noqa: F401

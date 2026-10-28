from django.apps import AppConfig


class AnalysisConfig(AppConfig):
    name = "analysis"
    verbose_name = "Investigation analysis"

    def ready(self):
        from . import signals  # noqa: F401

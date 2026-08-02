from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pandect.core"

    def ready(self) -> None:
        # Register tasks in web and management processes as well as consumers.
        from pandect.core import tasks  # noqa: F401

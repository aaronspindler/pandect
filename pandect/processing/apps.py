from django.apps import AppConfig


class ProcessingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pandect.processing"

    def ready(self) -> None:
        # Register tasks in web and management processes as well as consumers.
        from pandect.processing import (
            checks,  # noqa: F401
            tasks,  # noqa: F401
        )
        from pandect.processing.remote import tasks as remote_tasks  # noqa: F401

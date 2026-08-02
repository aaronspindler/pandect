"""Platform task examples and the convention for future background work."""

from huey.contrib.djhuey import task


@task()
def echo(value: str) -> str:
    """Return a value unchanged without external or persistent side effects."""
    return value

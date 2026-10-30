"""Harmless worker diagnostics, independent of case analysis tasks."""

from django.tasks import task


@task
def worker_probe(nonce):
    """Return a synthetic nonce so an operator can verify worker execution."""
    return nonce

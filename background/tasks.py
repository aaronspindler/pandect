"""Harmless diagnostics; product workflows do not enqueue background work yet."""

from django.tasks import task


@task
def worker_probe(nonce):
    """Return a synthetic nonce so an operator can verify worker execution."""
    return nonce

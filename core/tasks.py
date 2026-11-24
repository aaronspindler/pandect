"""Small application maintenance and worker diagnostics."""

from django.tasks import task
from django.utils import timezone


@task
def worker_probe(nonce):
    """Return a synthetic nonce so an operator can verify worker execution."""
    return nonce


@task
def expire_deletion_review(review_id):
    from core.models import DeletionReview

    DeletionReview.objects.filter(pk=review_id, expires_at__lte=timezone.now()).delete()

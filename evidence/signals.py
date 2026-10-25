import logging
from functools import partial

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import AudioEvidence, DocumentEvidence, ImageEvidence

logger = logging.getLogger(__name__)


def delete_original(storage, name):
    try:
        storage.delete(name)
    except Exception:
        # Generated keys support manual retry without logging original filenames,
        # file contents, or potentially sensitive storage exception messages.
        logger.error(
            "Evidence file cleanup failed; retry deleting storage key %s", name
        )


@receiver(post_delete, sender=ImageEvidence)
@receiver(post_delete, sender=AudioEvidence)
@receiver(post_delete, sender=DocumentEvidence)
def schedule_original_deletion(sender, instance, using, **kwargs):
    if instance.file:
        transaction.on_commit(
            partial(delete_original, instance.file.storage, instance.file.name),
            using=using,
            robust=True,
        )

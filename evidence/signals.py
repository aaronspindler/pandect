from functools import partial

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import (
    AudioEvidence,
    CallEvidenceAttachment,
    DocumentEvidence,
    ImageEvidence,
)
from .storage import delete_original


@receiver(post_delete, sender=ImageEvidence)
@receiver(post_delete, sender=AudioEvidence)
@receiver(post_delete, sender=DocumentEvidence)
@receiver(post_delete, sender=CallEvidenceAttachment)
def schedule_original_deletion(sender, instance, using, **kwargs):
    if instance.file:
        transaction.on_commit(
            partial(delete_original, instance.file.storage, instance.file.name),
            using=using,
            robust=True,
        )

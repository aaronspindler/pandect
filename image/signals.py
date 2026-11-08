from django.db.models.signals import post_delete

from core.storage import schedule_original_deletion
from image.models import ImageEvidence

post_delete.connect(
    schedule_original_deletion,
    sender=ImageEvidence,
    dispatch_uid="image_original_cleanup",
)

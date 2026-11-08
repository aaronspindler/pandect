from django.db.models.signals import post_delete

from audio.models import AudioEvidence
from core.storage import schedule_original_deletion

post_delete.connect(
    schedule_original_deletion,
    sender=AudioEvidence,
    dispatch_uid="audio_original_cleanup",
)

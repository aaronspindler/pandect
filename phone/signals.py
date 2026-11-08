from django.db.models.signals import post_delete

from core.storage import schedule_original_deletion
from phone.models import CallEvidenceAttachment

post_delete.connect(
    schedule_original_deletion,
    sender=CallEvidenceAttachment,
    dispatch_uid="phone_original_cleanup",
)

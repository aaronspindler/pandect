from django.db.models.signals import post_delete

from core.storage import schedule_original_deletion
from document.models import DocumentEvidence

post_delete.connect(
    schedule_original_deletion,
    sender=DocumentEvidence,
    dispatch_uid="document_original_cleanup",
)

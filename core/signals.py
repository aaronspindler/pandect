from django.db.models.signals import post_delete

from core.storage import schedule_original_deletion

from .models import AnalysisArtifact

post_delete.connect(
    schedule_original_deletion,
    sender=AnalysisArtifact,
    dispatch_uid="core_artifact_cleanup",
)

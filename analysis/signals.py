"""Reuse committed private-file cleanup for retained derived previews."""

from django.db.models.signals import post_delete

from evidence.signals import schedule_original_deletion

from .models import AnalysisArtifact

post_delete.connect(
    schedule_original_deletion,
    sender=AnalysisArtifact,
    dispatch_uid="analysis_preview_cleanup",
)

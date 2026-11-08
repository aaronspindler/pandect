from django.core.validators import MinValueValidator
from django.db import models

from core.models import FileEvidence


class DocumentEvidence(FileEvidence):
    page_count = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )

    class Meta(FileEvidence.Meta):
        constraints = [
            *FileEvidence.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(page_count__isnull=True)
                | models.Q(page_count__gt=0),
                name="evidence_document_pages_positive",
            ),
        ]

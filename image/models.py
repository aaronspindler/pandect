from django.core.validators import MinValueValidator
from django.db import models

from core.models import FileEvidence


class ImageEvidence(FileEvidence):
    width = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )
    height = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )

    class Meta(FileEvidence.Meta):
        constraints = [
            *FileEvidence.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(width__isnull=True) | models.Q(width__gt=0),
                name="evidence_image_width_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(height__isnull=True) | models.Q(height__gt=0),
                name="evidence_image_height_positive",
            ),
        ]

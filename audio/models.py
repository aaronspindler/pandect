from datetime import timedelta

from django.core.validators import MinValueValidator
from django.db import models

from core.models import FileEvidence


class AudioEvidence(FileEvidence):
    duration = models.DurationField(
        null=True, blank=True, validators=[MinValueValidator(timedelta())]
    )
    sample_rate = models.PositiveIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )
    channel_count = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )

    class Meta(FileEvidence.Meta):
        constraints = [
            *FileEvidence.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(duration__isnull=True)
                | models.Q(duration__gte=timedelta()),
                name="evidence_audio_duration_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(sample_rate__isnull=True)
                | models.Q(sample_rate__gt=0),
                name="evidence_audio_sample_rate_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(channel_count__isnull=True)
                | models.Q(channel_count__gt=0),
                name="evidence_audio_channels_positive",
            ),
        ]

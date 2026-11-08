import hashlib

from django.core.exceptions import ValidationError
from django.db import models

from core.models import Evidence


class TextEvidence(Evidence):
    body = models.TextField()
    language = models.CharField(max_length=35, blank=True)

    immutable_fields = (*Evidence.immutable_fields, "body")

    class Meta(Evidence.Meta):
        constraints = [
            *Evidence.Meta.constraints,
            models.CheckConstraint(
                condition=~models.Q(body=""), name="evidence_text_body_present"
            ),
        ]

    def _prepare_original(self):
        if not isinstance(self.body, str) or not self.body:
            raise ValidationError({"body": "Supply the original text."})
        self.sha256 = hashlib.sha256(self.body.encode("utf-8")).hexdigest()

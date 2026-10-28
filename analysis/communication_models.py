"Append-only context actually supplied by a supported retained message export."

from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models


class CommunicationContext(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    message = models.OneToOneField(
        "evidence.TextMessageEvidence",
        on_delete=models.CASCADE,
        related_name="communication_context",
    )
    source_document = models.ForeignKey(
        "evidence.DocumentEvidence",
        on_delete=models.RESTRICT,
        related_name="communication_contexts",
    )
    source_sha256 = models.CharField(max_length=64)
    source_row = models.PositiveIntegerField()
    source_start = models.PositiveIntegerField()
    source_end = models.PositiveIntegerField()
    parser_version = models.CharField(max_length=64)
    timezone = models.CharField(max_length=64)
    raw_sender = models.CharField(max_length=255)
    raw_recipient = models.CharField(max_length=255, blank=True)
    raw_timestamp = models.CharField(max_length=255, blank=True)
    native_message_id = models.CharField(max_length=255, blank=True)
    native_thread_id = models.CharField(max_length=255, blank=True)
    reply_to_id = models.CharField(max_length=255, blank=True)
    participants = models.JSONField(default=list, blank=True)
    account_identifiers = models.JSONField(default=dict, blank=True)
    attachments = models.JSONField(default=list, blank=True)
    source_context = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("source_document_id", "source_row")
        constraints = [
            models.UniqueConstraint(
                fields=("source_document", "source_row"),
                name="communication_source_row_once",
            ),
            models.CheckConstraint(
                condition=models.Q(source_row__gt=0),
                name="communication_source_row_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(source_end__gt=models.F("source_start")),
                name="communication_source_span_order",
            ),
            models.CheckConstraint(
                condition=models.Q(source_sha256__regex=r"^[0-9a-f]{64}$"),
                name="communication_source_hash_format",
            ),
        ]

    def __str__(self):
        return f"Imported communication context {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding or type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError(
                "Imported context cannot be replaced. Retain another "
                "source or review the conflict."
            )
        self.full_clean()
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.message_id and self.message.case_id != self.case_id:
            raise ValidationError("Message context must belong to its message's case.")
        if self.source_document_id and (
            self.source_document.case_id != self.case_id
            or self.source_document.sha256 != self.source_sha256
        ):
            raise ValidationError(
                "The retained export is unavailable or belongs to another case."
            )
        for field in ("participants", "attachments"):
            if not isinstance(getattr(self, field), list):
                raise ValidationError({field: "Source context must contain a list."})
        if not isinstance(self.account_identifiers, dict) or not isinstance(
            self.source_context, dict
        ):
            raise ValidationError(
                "Source account/context metadata must contain objects."
            )

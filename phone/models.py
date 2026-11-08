import hashlib
from datetime import UTC, timedelta
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, router
from django.utils import timezone

from core.models import Evidence
from core.storage import (
    canonical_bytes,
    file_integrity,
    new_file_save,
    original_upload_path,
)


class CommunicationEvidence(Evidence):
    """A retained communication between two case people, not verified identity."""

    sender = models.ForeignKey(
        "people.Person", on_delete=models.RESTRICT, related_name="%(class)s_sent"
    )
    recipient = models.ForeignKey(
        "people.Person", on_delete=models.RESTRICT, related_name="%(class)s_received"
    )
    occurred_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = (
        *Evidence.immutable_fields,
        "sender_id",
        "recipient_id",
        "occurred_at",
    )

    class Meta(Evidence.Meta):
        abstract = True

    def _check_original(self, using):
        super()._check_original(using)
        from people.models import Person

        errors = {}
        for name in ("sender", "recipient"):
            if (
                not Person.objects.using(using)
                .filter(pk=getattr(self, f"{name}_id"), case_id=self.case_id)
                .exists()
            ):
                errors[name] = "Choose a person in this case."
        if errors:
            raise ValidationError(errors)

    def original_data(self):
        data = {
            "schema": f"pandect.{self.communication_kind}.v1",
            "sender_id": str(self.sender_id),
            "recipient_id": str(self.recipient_id),
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat()
            if self.occurred_at is not None
            else None,
        }
        if self.communication_kind == "text-message":
            data["body"] = self.body
        else:
            data["duration_microseconds"] = (
                (
                    (self.duration.days * 86400 + self.duration.seconds) * 1000000
                    + self.duration.microseconds
                )
                if self.duration is not None
                else None
            )
        return data

    def original_bytes(self):
        return canonical_bytes(self.original_data())

    def _prepare_original(self):
        self.occurred_at = self._meta.get_field("occurred_at").clean(
            self.occurred_at, self
        )
        if self.occurred_at is not None and timezone.is_naive(self.occurred_at):
            raise ValidationError({"occurred_at": "Supply a time with a timezone."})
        if self.communication_kind == "text-message":
            if not isinstance(self.body, str) or not self.body:
                raise ValidationError({"body": "Supply the original message."})
        else:
            self.duration = self._meta.get_field("duration").clean(self.duration, self)
        self.sha256 = hashlib.sha256(self.original_bytes()).hexdigest()


class TextMessageEvidence(CommunicationEvidence):
    communication_kind = "text-message"
    body = models.TextField()
    immutable_fields = (*CommunicationEvidence.immutable_fields, "body")

    class Meta(CommunicationEvidence.Meta):
        constraints = [
            *CommunicationEvidence.Meta.constraints,
            models.CheckConstraint(
                condition=~models.Q(body=""), name="message_body_present"
            ),
        ]


class CallEvidence(CommunicationEvidence):
    duration = models.DurationField(
        null=True, blank=True, validators=[MinValueValidator(timedelta())]
    )
    immutable_fields = (*CommunicationEvidence.immutable_fields, "duration")

    class Meta(CommunicationEvidence.Meta):
        abstract = True
        constraints = [
            *CommunicationEvidence.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(duration__isnull=True)
                | models.Q(duration__gte=timedelta()),
                name="%(class)s_duration_nonnegative",
            ),
        ]


class PhoneCallEvidence(CallEvidence):
    communication_kind = "phone-call"


class FaceTimeEvidence(CallEvidence):
    communication_kind = "facetime"

    class Meta(CallEvidence.Meta):
        verbose_name = "FaceTime evidence"
        verbose_name_plural = "FaceTime evidence"


class CallEvidenceAttachment(models.Model):
    """An append-only transcript and/or recording belonging to one retained call."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    phone_call = models.ForeignKey(
        PhoneCallEvidence,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
    )
    facetime = models.ForeignKey(
        FaceTimeEvidence,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
    )
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    submission_id = models.UUIDField(unique=True, default=uuid4, editable=False)
    transcript = models.TextField(blank=True)
    file = models.FileField(upload_to=original_upload_path, max_length=500, blank=True)
    original_filename = models.CharField(max_length=255, editable=False, blank=True)
    byte_size = models.PositiveBigIntegerField(null=True, editable=False, blank=True)
    declared_content_type = models.CharField(max_length=255, editable=False, blank=True)
    audio_sha256 = models.CharField(max_length=64, editable=False, blank=True)
    sha256 = models.CharField(max_length=64, editable=False, blank=True)

    class Meta:
        ordering = ("created_at", "pk")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(phone_call__isnull=False, facetime__isnull=True)
                    | models.Q(phone_call__isnull=True, facetime__isnull=False)
                ),
                name="call_attachment_one_parent",
            ),
            models.CheckConstraint(
                condition=~models.Q(transcript="") | ~models.Q(file=""),
                name="call_attachment_content_present",
            ),
            models.CheckConstraint(
                condition=models.Q(sha256__regex=r"^[0-9a-f]{64}$"),
                name="call_attachment_sha256_format",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        file="",
                        original_filename="",
                        byte_size__isnull=True,
                        declared_content_type="",
                        audio_sha256="",
                    )
                    | (
                        ~models.Q(file="")
                        & ~models.Q(original_filename="")
                        & models.Q(
                            byte_size__isnull=False,
                            byte_size__gt=0,
                            audio_sha256__regex=r"^[0-9a-f]{64}$",
                        )
                    )
                ),
                name="call_attachment_audio_integrity",
            ),
        ]

    def __str__(self):
        return self.original_filename or "Supplied transcript"

    def save(
        self, *, force_insert=False, force_update=False, using=None, update_fields=None
    ):
        using = using or router.db_for_write(type(self), instance=self)
        self._validate_original(using)
        with new_file_save(self.file, using=using):
            return super().save(
                force_insert=self._state.adding,
                force_update=not self._state.adding,
                using=using,
                update_fields=update_fields,
            )

    @property
    def call(self):
        return self.phone_call if self.phone_call_id else self.facetime

    def original_data(self):
        return {
            "schema": "pandect.call-attachment.v1",
            "case_id": str(self.case_id),
            "kind": "phone-call" if self.phone_call_id else "facetime",
            "call_id": str(self.phone_call_id or self.facetime_id),
            "transcript": self.transcript,
            "audio": {
                "original_filename": self.original_filename,
                "byte_size": self.byte_size,
                "declared_content_type": self.declared_content_type,
                "sha256": self.audio_sha256,
            }
            if self.file
            else None,
        }

    def original_bytes(self):
        return canonical_bytes(self.original_data())

    def _validate_original(self, using):
        if bool(self.phone_call_id) == bool(self.facetime_id):
            raise ValidationError("Choose exactly one call.")
        model = PhoneCallEvidence if self.phone_call_id else FaceTimeEvidence
        if (
            not model.objects.using(using)
            .filter(
                pk=self.phone_call_id or self.facetime_id,
                case_id=self.case_id,
                sender__case_id=self.case_id,
                recipient__case_id=self.case_id,
            )
            .exists()
        ):
            raise ValidationError("Choose a call in this case.")
        if self._state.adding:
            if type(self).objects.using(using).filter(pk=self.pk).exists():
                raise ValidationError(
                    "New attachments cannot replace existing material."
                )
            if not self.case_id or not self.uploaded_by_id:
                raise ValidationError("An attachment requires a case and an uploader.")
            if not isinstance(self.transcript, str) or (
                not self.transcript and not self.file
            ):
                raise ValidationError("Supply a transcript, an audio file, or both.")
            if self.file:
                metadata = file_integrity(self.file)
                self.audio_sha256 = metadata.pop("sha256")
                for name, value in metadata.items():
                    setattr(self, name, value)
            else:
                self.original_filename = self.declared_content_type = (
                    self.audio_sha256
                ) = ""
                self.byte_size = None
            self.sha256 = hashlib.sha256(self.original_bytes()).hexdigest()
        else:
            fields = [
                f.attname for f in self._meta.concrete_fields if not f.primary_key
            ]
            original = (
                type(self)
                .objects.using(using)
                .filter(pk=self.pk)
                .values(*fields)
                .first()
            )
            if original is None or any(
                getattr(self, name) != value for name, value in original.items()
            ):
                raise ValidationError(
                    "Retained attachments cannot be changed. Add another attachment."
                )
            if self.file and not self.file._committed:
                raise ValidationError(
                    "Retained audio cannot be replaced. Add another attachment."
                )

    def clean(self):
        super().clean()
        self._validate_original(router.db_for_write(type(self), instance=self))


class CommunicationContext(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    message = models.OneToOneField(
        "phone.TextMessageEvidence",
        on_delete=models.CASCADE,
        related_name="communication_context",
    )
    source_document = models.ForeignKey(
        "document.DocumentEvidence",
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

import hashlib
from datetime import UTC, timedelta
from uuid import uuid4

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, router, transaction
from django.db.models.functions import Greatest, Least
from django.utils import timezone

from core.case_search.expressions import COMMON, search_document, word_vector
from core.models import Evidence
from core.storage import (
    canonical_bytes,
    file_integrity,
    new_file_save,
    original_upload_path,
)
from phone.grouping_models import (
    ConversationGrouping as ConversationGrouping,
)
from phone.grouping_models import (
    ConversationGroupingRequest as ConversationGroupingRequest,
)
from phone.grouping_models import (
    ConversationPairRevision as ConversationPairRevision,
)
from phone.grouping_models import (
    ConversationSession as ConversationSession,
)
from phone.grouping_models import (
    ConversationSessionMember as ConversationSessionMember,
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

    def _check_original(self, using, *, allow_unlinked=False):
        super()._check_original(using)
        from people.models import Person

        errors = {}
        for name in ("sender", "recipient"):
            person_id = getattr(self, f"{name}_id")
            if person_id is None and allow_unlinked:
                continue
            if (
                not Person.objects.using(using)
                .filter(pk=person_id, case_id=self.case_id)
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
    recipient = models.ForeignKey(
        "people.Person",
        on_delete=models.RESTRICT,
        related_name="%(class)s_received",
        null=True,
        blank=True,
    )
    group_source = models.ForeignKey(
        "document.DocumentEvidence",
        on_delete=models.RESTRICT,
        related_name="group_messages",
        null=True,
        blank=True,
        editable=False,
    )
    body = models.TextField()
    search_words = models.GeneratedField(
        expression=word_vector(search_document((*COMMON, "body"))),
        output_field=SearchVectorField(),
        db_persist=True,
    )
    immutable_fields = (
        *CommunicationEvidence.immutable_fields,
        "group_source_id",
        "body",
    )

    class Meta(CommunicationEvidence.Meta):
        indexes = [
            GinIndex(fields=["search_words"], name="srch_text_message_words"),
            models.Index(
                fields=("case", "occurred_at", "id"),
                condition=models.Q(is_archived=False, occurred_at__isnull=False),
                name="phone_message_case_time",
            ),
            models.Index(
                "case",
                Least("sender_id", "recipient_id"),
                Greatest("sender_id", "recipient_id"),
                "occurred_at",
                "id",
                condition=models.Q(is_archived=False, recipient__isnull=False),
                name="phone_active_pair_time",
            ),
        ]
        constraints = [
            *CommunicationEvidence.Meta.constraints,
            models.CheckConstraint(
                condition=~models.Q(body=""), name="message_body_present"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(recipient__isnull=False, group_source__isnull=True)
                    | models.Q(recipient__isnull=True, group_source__isnull=False)
                ),
                name="message_one_destination",
            ),
        ]

    def _check_original(self, using):
        if self.group_source_id is None:
            return super()._check_original(using)
        from document.models import DocumentEvidence

        if self.sender_id is None:
            raise ValidationError({"sender": "Choose a person in this case."})
        if self.recipient_id is not None:
            raise ValidationError({"recipient": "Choose a person or a group export."})
        super()._check_original(using, allow_unlinked=True)
        if (
            not DocumentEvidence.objects.using(using)
            .filter(pk=self.group_source_id, case_id=self.case_id)
            .exists()
        ):
            raise ValidationError("Choose a retained group export from this case.")

    def original_data(self):
        data = super().original_data()
        if self.group_source_id is not None:
            data["schema"] = "pandect.group-text-message.v1"
            data["group_source_id"] = str(self.group_source_id)
            del data["recipient_id"]
        return data


class CallImportSource(models.Model):
    """A line or account selected by an investigator for a case's CSV exports."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    label = models.CharField(max_length=255)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        ordering = ("label", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("case", "label"), name="phone_call_import_source_label"
            ),
            models.CheckConstraint(
                condition=models.Q(label__regex=r"\S"),
                name="phone_call_import_source_label_present",
            ),
        ]

    def __str__(self):
        return self.label


class CallHistoryImport(models.Model):
    """The retained source and reviewed interpretation for one call history CSV."""

    class Status(models.TextChoices):
        PREPARING = "preparing", "Preparing review"
        REVIEW = "review", "Ready to review"
        PUBLISHING = "publishing", "Publishing calls"
        COMPLETED = "completed", "Complete"
        PREPARE_FAILED = "prepare_failed", "Preparation needs attention"
        PUBLISH_FAILED = "publish_failed", "Publication needs attention"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    document = models.ForeignKey(
        "document.DocumentEvidence",
        on_delete=models.RESTRICT,
        related_name="call_imports",
    )
    source = models.ForeignKey(
        CallImportSource, on_delete=models.RESTRICT, related_name="imports"
    )
    source_sha256 = models.CharField(max_length=64, editable=False)
    source_label = models.CharField(max_length=255, editable=False)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_call_imports",
    )
    submission_id = models.UUIDField(default=uuid4, editable=False)
    timezone = models.CharField(max_length=100)
    parser_version = models.CharField(max_length=50)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PREPARING
    )
    error = models.CharField(max_length=50, blank=True)
    attempt_id = models.UUIDField(default=uuid4, editable=False)
    attempts = models.PositiveSmallIntegerField(default=1)
    is_running = models.BooleanField(default=False)
    selection = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("source", "source_sha256"), name="phone_call_import_source_file"
            ),
            models.UniqueConstraint(
                fields=("case", "uploaded_by", "submission_id"),
                name="phone_call_import_submission_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(source_sha256__regex=r"^[0-9a-f]{64}$"),
                name="phone_call_import_source_sha256_format",
            ),
            models.CheckConstraint(
                condition=models.Q(source_label__regex=r"\S"),
                name="phone_call_import_source_snapshot_present",
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__gte=1, attempts__lte=3),
                name="phone_call_import_attempt_bound",
            ),
        ]

    def __str__(self):
        return f"Call history import {self.pk}"

    def save(self, *args, **kwargs):
        self.clean()
        kwargs["force_insert" if self._state.adding else "force_update"] = True
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from document.models import DocumentEvidence

        source = CallImportSource.objects.filter(
            case_id=self.case_id, pk=self.source_id
        )
        if self._state.adding:
            source = source.filter(label=self.source_label)
        if (
            not source.exists()
            or not DocumentEvidence.objects.filter(
                case_id=self.case_id, pk=self.document_id, sha256=self.source_sha256
            ).exists()
        ):
            raise ValidationError("Choose a source line and original in this case.")
        if not self._state.adding:
            fields = (
                "case_id",
                "source_id",
                "source_label",
                "document_id",
                "source_sha256",
                "uploaded_by_id",
                "submission_id",
                "timezone",
                "parser_version",
            )
            stored = type(self).objects.filter(pk=self.pk).values(*fields).first()
            if stored is None or any(
                getattr(self, field) != value for field, value in stored.items()
            ):
                raise ValidationError("The retained import source cannot change.")


class CallHistoryRow(models.Model):
    """One source row, its interpretation, and its publication disposition."""

    class Disposition(models.TextChoices):
        PENDING = "", "Ready"
        PUBLISHED = "published", "Published"
        SKIPPED = "skipped", "Skipped possible overlap"
        HELD = "held", "Held in review"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    batch = models.ForeignKey(
        CallHistoryImport, on_delete=models.CASCADE, related_name="rows"
    )
    number = models.PositiveIntegerField()
    raw = models.JSONField(default=dict)
    kind = models.CharField(max_length=20, blank=True)
    direction = models.CharField(max_length=8, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    duration = models.DurationField(null=True, blank=True)
    identifier = models.CharField(max_length=512, blank=True)
    fingerprint = models.CharField(max_length=64, blank=True)
    issue = models.CharField(max_length=255, blank=True)
    disposition = models.CharField(
        max_length=10,
        choices=Disposition.choices,
        default=Disposition.PENDING,
        blank=True,
    )
    overlap_candidates = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ("number",)
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "number"), name="phone_call_import_row_number"
            ),
        ]
        indexes = [
            models.Index(fields=("batch", "identifier")),
            models.Index(
                fields=("fingerprint",),
                condition=models.Q(disposition="published"),
                name="phone_call_import_overlap",
            ),
        ]

    def __str__(self):
        return f"Call history import {self.batch_id}, row {self.number}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            immutable = (
                "batch_id",
                "number",
                "raw",
                "kind",
                "direction",
                "occurred_at",
                "duration",
                "identifier",
                "fingerprint",
                "issue",
            )
            original = type(self).objects.filter(pk=self.pk).values(*immutable).first()
            if original is None or any(
                getattr(self, field) != value for field, value in original.items()
            ):
                raise ValidationError("A retained CSV row cannot change.")
        kwargs["force_insert" if self._state.adding else "force_update"] = True
        return super().save(*args, **kwargs)


class CallPersonLink(models.Model):
    """An attributed investigator association, separate from the source row."""

    class Role(models.TextChoices):
        SENDER = "sender", "Sender"
        RECIPIENT = "recipient", "Recipient"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    row = models.ForeignKey(
        CallHistoryRow, on_delete=models.CASCADE, related_name="person_links"
    )
    role = models.CharField(max_length=9, choices=Role.choices)
    person = models.ForeignKey("people.Person", on_delete=models.RESTRICT)
    linked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("row", "role"), name="phone_call_import_person_role"
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=("sender", "recipient")),
                name="phone_call_import_person_role_known",
            ),
        ]

    def __str__(self):
        return f"Investigator {self.role} link {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A retained investigator link cannot change.")
        from people.models import Person

        case = CallHistoryRow.objects.filter(pk=self.row_id).values("batch__case_id")
        if not Person.objects.filter(pk=self.person_id, case_id__in=case).exists():
            raise ValidationError("Choose a person in the call's case.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)


class CallEvidence(CommunicationEvidence):
    sender = models.ForeignKey(
        "people.Person",
        on_delete=models.RESTRICT,
        related_name="%(class)s_sent",
        null=True,
        blank=True,
    )
    recipient = models.ForeignKey(
        "people.Person",
        on_delete=models.RESTRICT,
        related_name="%(class)s_received",
        null=True,
        blank=True,
    )
    import_row = models.OneToOneField(
        CallHistoryRow,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        editable=False,
        related_name="%(class)s",
    )
    duration = models.DurationField(
        null=True, blank=True, validators=[MinValueValidator(timedelta())]
    )
    immutable_fields = (
        *CommunicationEvidence.immutable_fields,
        "duration",
        "import_row_id",
    )

    class Meta(CommunicationEvidence.Meta):
        abstract = True
        constraints = [
            *CommunicationEvidence.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(import_row__isnull=False)
                | models.Q(sender__isnull=False, recipient__isnull=False),
                name="%(class)s_manual_parties_present",
            ),
            models.CheckConstraint(
                condition=models.Q(duration__isnull=True)
                | models.Q(duration__gte=timedelta()),
                name="%(class)s_duration_nonnegative",
            ),
        ]

    def _check_original(self, using):
        super()._check_original(using, allow_unlinked=self.import_row_id is not None)
        if (
            self.import_row_id
            and not CallHistoryRow.objects.using(using)
            .filter(
                pk=self.import_row_id,
                kind=self.communication_kind,
                occurred_at=self.occurred_at,
                occurred_at__isnull=False,
                duration=self.duration,
                issue="",
                disposition__in=(
                    CallHistoryRow.Disposition.PENDING,
                    CallHistoryRow.Disposition.PUBLISHED,
                ),
                batch__case_id=self.case_id,
                batch__source__case_id=self.case_id,
                batch__document__case_id=self.case_id,
                batch__document__sha256=models.F("batch__source_sha256"),
            )
            .exists()
        ):
            raise ValidationError("The call does not match its retained source row.")

    def original_data(self):
        data = super().original_data()
        if not self.import_row_id:
            return data
        row = self.import_row
        batch = row.batch
        return {
            "schema": f"pandect.{self.communication_kind}.csv.v1",
            "occurred_at": data["occurred_at"],
            "duration_microseconds": data["duration_microseconds"],
            "source": {
                "document_id": str(batch.document_id),
                "sha256": batch.source_sha256,
                "source_id": str(batch.source_id),
                "source_label": batch.source_label,
                "row": row.number,
                "parser_version": batch.parser_version,
                "timezone": batch.timezone,
            },
            "row": row.raw,
        }


class PhoneCallEvidence(CallEvidence):
    communication_kind = "phone-call"

    class Meta(CallEvidence.Meta):
        indexes = [
            models.Index(
                fields=("case", "occurred_at", "id"),
                condition=models.Q(is_archived=False, occurred_at__isnull=False),
                name="phone_call_case_time",
            ),
        ]


class FaceTimeEvidence(CallEvidence):
    communication_kind = "facetime"

    class Meta(CallEvidence.Meta):
        verbose_name = "FaceTime evidence"
        verbose_name_plural = "FaceTime evidence"
        indexes = [
            models.Index(
                fields=("case", "occurred_at", "id"),
                condition=models.Q(is_archived=False, occurred_at__isnull=False),
                name="phone_facetime_case_time",
            ),
        ]


@transaction.atomic
def link_imported_person(item, *, role, person, actor):
    """Link an unassigned party while preserving the call's original bytes."""
    from cases.permissions import get_case
    from core.evidence.queries import get_evidence
    from people.models import Person

    if role not in CallPersonLink.Role.values or not isinstance(item, CallEvidence):
        raise ValidationError("Choose a call party to link.")
    case = get_case(actor, item.case_id, edit=True, lock=True)
    call = get_evidence(case, item.communication_kind, item.pk, lock=True)
    if not call.import_row_id or call.is_archived:
        raise ValidationError("Choose an active imported call.")
    if not Person.objects.filter(pk=person.pk, case=case).exists():
        raise ValidationError("Choose a person in this case.")

    link = CallPersonLink.objects.filter(row_id=call.import_row_id, role=role)
    field = f"{role}_id"
    if getattr(call, field) == person.pk and (
        prior := link.filter(person=person).first()
    ):
        return prior
    if getattr(call, field) is not None or link.exists():
        raise ValidationError("This call party already has an investigator link.")

    assertion = CallPersonLink.objects.create(
        row_id=call.import_row_id, role=role, person=person, linked_by=actor
    )
    # Normal evidence saves reject changes to either participant. This attributed
    # update is available only for imported calls whose original is the CSV row.
    type(call).objects.filter(pk=call.pk).update(
        **{field: person.pk, "edited_at": timezone.now()}
    )
    return assertion


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
    submission_id = models.UUIDField(default=uuid4, editable=False)
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
            models.UniqueConstraint(
                fields=("case", "uploaded_by", "submission_id"),
                name="call_attachment_submission_scope",
            ),
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
    import_run = models.ForeignKey(
        "core.AnalysisRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=False,
        related_name="imported_message_contexts",
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
        indexes = [
            models.Index(
                fields=("import_run", "source_row"), name="communication_import_row"
            ),
            models.Index(
                fields=("source_document", "native_message_id"),
                name="communication_native_id",
            ),
        ]
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
        if self.message_id:
            if self.message.case_id != self.case_id:
                raise ValidationError(
                    "Message context must belong to its message's case."
                )
            if (
                self.message.group_source_id is not None
                and self.message.group_source_id != self.source_document_id
            ):
                raise ValidationError(
                    "Message context must use its retained group export."
                )
        if self.source_document_id and (
            self.source_document.case_id != self.case_id
            or self.source_document.sha256 != self.source_sha256
        ):
            raise ValidationError(
                "The retained export is unavailable or belongs to another case."
            )
        if self.import_run_id:
            run = self.import_run
            if (
                run.case_id != self.case_id
                or run.operation != "imessage_import"
                or run.parameters.get("phase") != "import"
                or len(run.inputs) != 1
                or run.inputs[0].get("kind") != "document"
                or run.inputs[0].get("evidence_id") != str(self.source_document_id)
                or run.inputs[0].get("sha256") != self.source_sha256
            ):
                raise ValidationError(
                    "Import membership must belong to this case and retained export."
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

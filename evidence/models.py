import hashlib
import json
from datetime import UTC, timedelta
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, router
from django.utils import timezone


def original_upload_path(instance, filename):
    """Keep user filenames out of private, case-scoped storage keys."""
    return (
        f"evidence/cases/{instance.case_id}/{instance._meta.model_name}/{uuid4().hex}"
    )


def file_integrity(original, declared_content_type=""):
    """Inspect a new file without changing its position or trusting metadata."""
    if not original or original._committed:
        raise ValidationError({"file": "Supply a new named file, not a storage path."})
    content = original.file
    position = content.tell()
    digest = hashlib.sha256()
    byte_size = 0
    try:
        content.seek(0)
        while chunk := content.read(64 * 1024):
            digest.update(chunk)
            byte_size += len(chunk)
    finally:
        content.seek(position)
    if not byte_size:
        raise ValidationError({"file": "The original file cannot be empty."})
    return {
        "sha256": digest.hexdigest(),
        "byte_size": byte_size,
        "original_filename": original.name.replace("\\", "/").rsplit("/", 1)[-1],
        "declared_content_type": (
            getattr(content, "content_type", None) or declared_content_type or ""
        ),
    }


def canonical_bytes(data):
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class Evidence(models.Model):
    """Shared case boundary, provenance, and integrity of original material."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_items",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(auto_now=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    source = models.CharField(max_length=255, blank=True)
    source_url = models.URLField(max_length=2048, blank=True)
    source_description = models.TextField(blank=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, editable=False, blank=True)
    submission_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    is_archived = models.BooleanField(default=False)
    archive_changed_at = models.DateTimeField(null=True, blank=True, editable=False)
    archive_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="%(app_label)s_%(class)s_archive_changes",
    )

    immutable_fields = (
        "case_id",
        "uploaded_by_id",
        "created_at",
        "sha256",
        "submission_id",
    )

    class Meta:
        abstract = True
        ordering = ("-created_at", "-pk")
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(title=""),
                name="%(app_label)s_%(class)s_title_present",
            ),
            models.CheckConstraint(
                condition=models.Q(sha256__regex=r"^[0-9a-f]{64}$"),
                name="%(app_label)s_%(class)s_sha256_format",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        archive_changed_at__isnull=True,
                        archive_changed_by__isnull=True,
                        is_archived=False,
                    )
                    | models.Q(
                        archive_changed_at__isnull=False,
                        archive_changed_by__isnull=False,
                    )
                ),
                name="%(app_label)s_%(class)s_archive_attribution",
            ),
        ]

    def __str__(self):
        return self.title

    def save(
        self, *, force_insert=False, force_update=False, using=None, update_fields=None
    ):
        if update_fields is not None:
            update_fields = frozenset(update_fields)
            if not update_fields:
                return
            update_fields |= {"edited_at"}
        using = using or router.db_for_write(type(self), instance=self)
        self._check_original(using)
        if self._state.adding:
            if not self.case_id or not self.uploaded_by_id:
                raise ValidationError("Evidence requires a case and an uploader.")
            self._prepare_original()
            # A preassigned UUID must never turn a new record into an UPDATE.
            force_insert = True
        else:
            # Prevent Django's update-then-insert fallback after concurrent deletion.
            force_update = True
            if "edited_at" in self.get_deferred_fields():
                self.edited_at = timezone.now()
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def _prepare_original(self):
        raise NotImplementedError

    def _check_original(self, using):
        if self._state.adding:
            if type(self)._base_manager.using(using).filter(pk=self.pk).exists():
                raise ValidationError("New evidence cannot replace an existing record.")
            return
        original = (
            type(self)
            ._base_manager.using(using)
            .filter(pk=self.pk)
            .values(*self.immutable_fields)
            .first()
        )
        if original is None:
            raise ValidationError(
                "This evidence no longer exists. Create a new record."
            )
        errors = {
            self._meta.get_field(field).name: "Original evidence cannot be changed."
            for field, value in original.items()
            if getattr(self, field) != value
        }
        if isinstance(self, FileEvidence) and not self.file._committed:
            errors["file"] = (
                "Original evidence cannot be replaced. Create a new record."
            )
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        using = router.db_for_write(type(self), instance=self)
        self._check_original(using)
        if self._state.adding:
            self._prepare_original()


class FileEvidence(Evidence):
    """An original file; declared media metadata is not verified by a parser."""

    file = models.FileField(upload_to=original_upload_path, max_length=500)
    original_filename = models.CharField(max_length=255, editable=False, blank=True)
    byte_size = models.PositiveBigIntegerField(editable=False, blank=True)
    declared_content_type = models.CharField(max_length=255, blank=True)

    immutable_fields = (
        *Evidence.immutable_fields,
        "file",
        "original_filename",
        "byte_size",
        "declared_content_type",
    )

    class Meta(Evidence.Meta):
        abstract = True
        constraints = [
            *Evidence.Meta.constraints,
            models.CheckConstraint(
                condition=~models.Q(file=""),
                name="%(app_label)s_%(class)s_file_present",
            ),
            models.CheckConstraint(
                condition=~models.Q(original_filename=""),
                name="%(app_label)s_%(class)s_filename_present",
            ),
            models.CheckConstraint(
                condition=models.Q(byte_size__gt=0),
                name="%(app_label)s_%(class)s_file_not_empty",
            ),
        ]

    def _prepare_original(self):
        for name, value in file_integrity(
            self.file, self.declared_content_type
        ).items():
            setattr(self, name, value)


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


class LocationContent(Evidence):
    """An immutable, investigator-supplied location observation."""

    address = models.TextField(blank=True, max_length=20000)
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    observed_at = models.DateTimeField(null=True, blank=True)

    immutable_fields = (
        *Evidence.immutable_fields,
        "address",
        "latitude",
        "longitude",
        "observed_at",
    )

    class Meta(Evidence.Meta):
        abstract = True
        constraints = [
            *Evidence.Meta.constraints,
            models.CheckConstraint(
                condition=models.Q(address__regex=r"\S")
                | models.Q(latitude__isnull=False, longitude__isnull=False),
                name="%(app_label)s_%(class)s_location_present",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__isnull=True, longitude__isnull=True)
                | models.Q(latitude__isnull=False, longitude__isnull=False),
                name="%(app_label)s_%(class)s_coordinate_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(latitude__isnull=True)
                | models.Q(latitude__gte=-90, latitude__lte=90),
                name="%(app_label)s_%(class)s_latitude_range",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__isnull=True)
                | models.Q(longitude__gte=-180, longitude__lte=180),
                name="%(app_label)s_%(class)s_longitude_range",
            ),
        ]

    def original_data(self):
        """Versioned JSON values shared by hashing and private downloads."""
        return {
            "schema": "pandect.location.v1",
            "address": self.address,
            "latitude": (
                format(self.latitude or 0, ".6f") if self.latitude is not None else None
            ),
            "longitude": (
                format(self.longitude or 0, ".6f")
                if self.longitude is not None
                else None
            ),
            "observed_at": (
                self.observed_at.astimezone(UTC).isoformat()
                if self.observed_at is not None
                else None
            ),
        }

    def original_bytes(self):
        return canonical_bytes(self.original_data())

    def _prepare_original(self):
        for name in ("address", "latitude", "longitude", "observed_at"):
            field = self._meta.get_field(name)
            setattr(self, name, field.clean(getattr(self, name), self))
        if (self.latitude is None) != (self.longitude is None):
            raise ValidationError("Enter both latitude and longitude, or neither.")
        if not self.address.strip() and self.latitude is None:
            raise ValidationError("Enter an address or both coordinates.")
        if self.observed_at is not None and timezone.is_naive(self.observed_at):
            self.observed_at = timezone.make_aware(self.observed_at)
        self.sha256 = hashlib.sha256(self.original_bytes()).hexdigest()


class LocationEvidence(LocationContent):
    pass


class PersonLocationEvidence(LocationContent):
    """A reported association with a case person, not proof of presence."""

    person = models.ForeignKey(
        "people.Person", on_delete=models.RESTRICT, related_name="location_evidence"
    )

    immutable_fields = (*LocationContent.immutable_fields, "person_id")

    def _check_original(self, using):
        super()._check_original(using)
        from people.models import Person

        if (
            not self.person_id
            or not Person.objects.using(using)
            .filter(pk=self.person_id, case_id=self.case_id)
            .exists()
        ):
            raise ValidationError({"person": "Choose a person in this case."})

    def original_data(self):
        return {
            **super().original_data(),
            "schema": "pandect.person-location.v1",
            "person_id": str(self.person_id),
        }


class VehicleLocationImport(models.Model):
    """A retained CSV and the interpretation used for one atomic import."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    document = models.OneToOneField(DocumentEvidence, on_delete=models.RESTRICT)
    timezone = models.CharField(max_length=100)
    parser_version = models.CharField(max_length=50, default="asset-location.v1")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    vehicle_mapping = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")

    def __str__(self):
        return f"Vehicle location import {self.pk}"

    def save(self, *args, **kwargs):
        self.clean()
        if self._state.adding:
            kwargs["force_insert"] = True
        else:
            kwargs["force_update"] = True
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if not DocumentEvidence.objects.filter(
            pk=self.document_id,
            case_id=self.case_id,
            uploaded_by_id=self.uploaded_by_id,
        ).exists():
            raise ValidationError("Choose a source document uploaded in this case.")
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values().first()
            if original is None:
                raise ValidationError("This import no longer exists.")
            immutable = (
                "case_id",
                "uploaded_by_id",
                "document_id",
                "timezone",
                "parser_version",
                "created_at",
            )
            if original["completed_at"] is not None:
                immutable += ("completed_at", "vehicle_mapping")
            if any(getattr(self, name) != original[name] for name in immutable):
                raise ValidationError(
                    "The retained import interpretation cannot change."
                )


class VehicleLocationEvidence(LocationContent):
    """A reported vehicle association, optionally extracted from a retained CSV."""

    vehicle = models.ForeignKey(
        "vehicles.Vehicle", on_delete=models.RESTRICT, related_name="location_evidence"
    )
    import_batch = models.ForeignKey(
        VehicleLocationImport,
        on_delete=models.RESTRICT,
        related_name="observations",
        null=True,
        blank=True,
    )
    source_row = models.PositiveIntegerField(null=True, blank=True)
    raw_row = models.JSONField(default=dict, blank=True)
    speed_kmh = models.DecimalField(
        max_digits=9,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    received_at = models.DateTimeField(null=True, blank=True)

    immutable_fields = (
        *LocationContent.immutable_fields,
        "vehicle_id",
        "import_batch_id",
        "source_row",
        "raw_row",
        "speed_kmh",
        "received_at",
    )

    class Meta(LocationContent.Meta):
        constraints = [
            *LocationContent.Meta.constraints,
            models.UniqueConstraint(
                fields=("import_batch", "source_row"),
                name="evidence_vehicle_import_row",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(import_batch__isnull=True, source_row__isnull=True)
                    | models.Q(
                        import_batch__isnull=False,
                        source_row__isnull=False,
                        source_row__gte=2,
                    )
                ),
                name="evidence_vehicle_import_source_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(speed_kmh__isnull=True) | models.Q(speed_kmh__gte=0),
                name="evidence_vehicle_speed_nonnegative",
            ),
        ]

    def _check_original(self, using):
        super()._check_original(using)
        from vehicles.models import Vehicle

        if (
            not Vehicle.objects.using(using)
            .filter(pk=self.vehicle_id, case_id=self.case_id)
            .exists()
        ):
            raise ValidationError({"vehicle": "Choose a vehicle in this case."})
        if (
            self.import_batch_id
            and not VehicleLocationImport.objects.using(using)
            .filter(
                pk=self.import_batch_id,
                case_id=self.case_id,
                document__case_id=self.case_id,
            )
            .exists()
        ):
            raise ValidationError("Choose an import in this case.")

    def _prepare_original(self):
        for name in ("speed_kmh", "received_at", "source_row", "raw_row"):
            field = self._meta.get_field(name)
            setattr(self, name, field.clean(getattr(self, name), self))
        if self.speed_kmh == 0:
            # PostgreSQL numeric discards a zero's sign; hash its retained form.
            self.speed_kmh = abs(self.speed_kmh)
        if (self.import_batch_id is None) != (self.source_row is None):
            raise ValidationError("An imported observation requires its source row.")
        if self.received_at is not None and timezone.is_naive(self.received_at):
            raise ValidationError("The source receipt time requires a timezone.")
        super()._prepare_original()

    def original_data(self):
        provenance = None
        if self.import_batch_id:
            batch = self.import_batch
            provenance = {
                "import_id": str(batch.pk),
                "document_id": str(batch.document_id),
                "document_sha256": batch.document.sha256,
                "row": self.source_row,
                "timezone": batch.timezone,
                "parser_version": batch.parser_version,
            }
        return {
            **super().original_data(),
            "schema": "pandect.vehicle-location.v1",
            "vehicle_id": str(self.vehicle_id),
            "import": provenance,
            "raw_row": self.raw_row,
            "speed_kmh": format(self.speed_kmh, ".3f")
            if self.speed_kmh is not None
            else None,
            "received_at": self.received_at.astimezone(UTC).isoformat()
            if self.received_at
            else None,
        }


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

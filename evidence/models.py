import hashlib
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, router
from django.utils import timezone


def original_upload_path(instance, filename):
    """Keep user filenames out of private, case-scoped storage keys."""
    return (
        f"evidence/cases/{instance.case_id}/{instance._meta.model_name}/{uuid4().hex}"
    )


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
    source_url = models.URLField(max_length=2048, blank=True)
    source_description = models.TextField(blank=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, editable=False, blank=True)

    immutable_fields = ("case_id", "uploaded_by_id", "created_at", "sha256")

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
        if not self.file or self.file._committed:
            raise ValidationError(
                {"file": "Supply a new named file, not a storage path."}
            )
        content = self.file.file
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
        self.sha256 = digest.hexdigest()
        self.byte_size = byte_size
        self.original_filename = self.file.name.replace("\\", "/").rsplit("/", 1)[-1]
        self.declared_content_type = (
            getattr(content, "content_type", None) or self.declared_content_type or ""
        )


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

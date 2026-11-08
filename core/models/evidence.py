from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, router
from django.utils import timezone

from core.storage import file_integrity, new_file_save, original_upload_path


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

    def save(self, *, using=None, **kwargs):
        using = using or router.db_for_write(type(self), instance=self)
        with new_file_save(self.file, using=using):
            return super().save(using=using, **kwargs)

    def _prepare_original(self):
        for name, value in file_integrity(
            self.file, self.declared_content_type
        ).items():
            setattr(self, name, value)

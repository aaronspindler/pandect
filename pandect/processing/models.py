"""Durable state and provenance for asynchronous evidence processing."""

import uuid
from pathlib import PurePath

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q

from pandect.catalog.models import CatalogItem, validate_timezone_aware

validate_sha256 = RegexValidator(
    regex=r"\A[0-9a-f]{64}\Z",
    message="Enter a lowercase hexadecimal SHA-256 digest.",
    code="invalid_sha256",
)


def derived_artifact_upload_to(instance, filename: str) -> str:
    """Keep derived files grouped by source item and immutable run."""
    basename = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    basename = basename[:255] or "artifact"
    return (
        f"derived/{instance.catalog_item_id}/{instance.stage.run_id}/"
        f"{instance.id}-{basename}"
    )


class ImmutableEvidenceQuerySet(models.QuerySet):
    """Reject bulk operations that would rewrite append-only evidence."""

    def update(self, **kwargs):
        raise TypeError(f"{self.model.__name__} records are append-only.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise TypeError(f"{self.model.__name__} records are append-only.")

    def delete(self):
        raise TypeError(f"{self.model.__name__} records are append-only.")


class ImmutableEvidenceModel(models.Model):
    """Prevent ordinary code from rewriting an append-only evidence record.

    Parent relationships on concrete evidence models must use PROTECT: Django's
    cascade collector does not call these instance/queryset deletion guards.
    """

    objects = ImmutableEvidenceQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError(f"{type(self).__name__} records are append-only.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError(f"{type(self).__name__} records are append-only.")


class ProcessingRun(models.Model):
    """One versioned attempt to derive evidence from a canonical item input."""

    class Trigger(models.TextChoices):
        AUTOMATIC = "automatic", "Automatic"
        MANUAL = "manual", "Manual"
        BACKFILL = "backfill", "Backfill"

    class Scope(models.TextChoices):
        LOCAL = "local", "Local"
        REMOTE = "remote", "Remote"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        SUPERSEDED = "superseded", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    catalog_item = models.ForeignKey(
        CatalogItem,
        on_delete=models.CASCADE,
        related_name="processing_runs",
    )
    trigger = models.CharField(max_length=16, choices=Trigger.choices)
    scope = models.CharField(max_length=16, choices=Scope.choices, default=Scope.LOCAL)
    source_fingerprint = models.CharField(
        max_length=64,
        validators=[validate_sha256],
    )
    source_size = models.PositiveBigIntegerField()
    processor_version = models.CharField(max_length=64, default="1")
    configuration = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(
                fields=["catalog_item", "-created_at"],
                name="processing_run_item_idx",
            ),
            models.Index(
                fields=["status", "created_at"],
                name="processing_run_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True)
                | Q(
                    status__in=(
                        "succeeded",
                        "partial",
                        "failed",
                        "superseded",
                    )
                ),
                name="processing_run_finished_terminal",
            )
        ]

    def __str__(self) -> str:
        return f"{self.catalog_item} processing run {self.pk}"


class ProcessingStage(models.Model):
    """A durable, idempotently claimable unit of work within a run."""

    class Kind(models.TextChoices):
        TEXT_EXTRACTION = "text_extraction", "Text extraction"
        WRITING_STYLE = "writing_style", "Writing style"
        WRITING_IDENTITY = "writing_identity", "Writing identity"
        IMAGE_METADATA = "image_metadata", "Image metadata"
        IMAGE_OCR = "image_ocr", "Image OCR"
        IMAGE_QR = "image_qr", "Image QR extraction"
        IMAGE_VISUAL = "image_visual", "Image visual analysis"
        FACE_ANALYSIS = "face_analysis", "Face analysis"
        FACE_IDENTITY = "face_identity", "Face identity"
        VIDEO_METADATA = "video_metadata", "Video metadata"
        VIDEO_SUBTITLES = "video_subtitles", "Video subtitles"
        VIDEO_KEYFRAMES = "video_keyframes", "Video keyframes"
        VIDEO_AUDIO = "video_audio", "Video audio extraction"
        VIDEO_FRAME_ANALYSIS = "video_frame_analysis", "Video frame analysis"
        AUDIO_METADATA = "audio_metadata", "Audio metadata"
        AUDIO_SPLIT = "audio_split", "Audio splitting"
        AUDIO_ACOUSTICS = "audio_acoustics", "Audio acoustic analysis"
        VOICE_ANALYSIS = "voice_analysis", "Voice analysis"
        VOICE_IDENTITY = "voice_identity", "Voice identity"
        REMOTE_SEMANTIC = "remote_semantic", "Remote semantic analysis"
        REMOTE_IMAGE_DESCRIPTION = (
            "remote_image_description",
            "Remote image description",
        )
        REMOTE_TRANSCRIPTION = "remote_transcription", "Remote transcription"
        REMOTE_VIDEO_DESCRIPTION = (
            "remote_video_description",
            "Remote video description",
        )
        REMOTE_AGGREGATE = (
            "remote_aggregate",
            "Remote result aggregation (local)",
        )

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        SUPERSEDED = "superseded", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        ProcessingRun,
        on_delete=models.CASCADE,
        related_name="stages",
    )
    kind = models.CharField(max_length=48, choices=Kind.choices)
    unit_key = models.CharField(max_length=128, default="default")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=2)
    task_id = models.CharField(max_length=64, blank=True)
    priority = models.SmallIntegerField(default=0)
    processor_name = models.CharField(max_length=128, blank=True)
    processor_version = models.CharField(max_length=64, default="1")
    schema_version = models.PositiveSmallIntegerField(default=1)
    failure_code = models.CharField(max_length=64, blank=True)
    failure_detail = models.CharField(max_length=512, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "kind", "unit_key"],
                name="processing_unique_stage_unit",
            ),
            models.CheckConstraint(
                condition=Q(max_attempts__gte=1),
                name="processing_stage_has_attempt",
            ),
            models.CheckConstraint(
                condition=Q(attempts__lte=F("max_attempts")),
                name="processing_stage_attempt_limit",
            ),
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True)
                | Q(
                    status__in=(
                        "succeeded",
                        "failed",
                        "skipped",
                        "superseded",
                    )
                ),
                name="processing_stage_finished_terminal",
            ),
        ]
        indexes = [
            models.Index(
                fields=["run", "status"],
                name="processing_stage_run_idx",
            ),
            models.Index(
                fields=["status", "lease_expires_at"],
                name="processing_stage_lease_idx",
            ),
            models.Index(
                fields=["kind", "status"],
                name="processing_stage_kind_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} ({self.unit_key})"


class ProcessingResult(ImmutableEvidenceModel):
    """An append-only, typed result produced by one processing stage."""

    class Kind(models.TextChoices):
        MESSAGE_TEXT_PROFILE = "message_text_profile", "Message text profile"
        TEXT_EXTRACTION = "text_extraction", "Text extraction"
        WRITING_STYLE = "writing_style", "Writing style"
        AUTHORSHIP_CANDIDATES = "authorship_candidates", "Authorship candidates"
        IMAGE_METADATA = "image_metadata", "Image metadata"
        OCR = "ocr", "OCR"
        QR_CODES = "qr_codes", "QR codes"
        IMAGE_VISUAL = "image_visual", "Image visual analysis"
        FACE_OBSERVATIONS = "face_observations", "Face observations"
        FACE_CANDIDATES = "face_candidates", "Face candidates"
        VIDEO_METADATA = "video_metadata", "Video metadata"
        SUBTITLES = "subtitles", "Subtitles"
        KEYFRAMES = "keyframes", "Keyframes"
        AUDIO_STREAMS = "audio_streams", "Audio streams"
        AUDIO_METADATA = "audio_metadata", "Audio metadata"
        AUDIO_SPLITS = "audio_splits", "Audio splits"
        ACOUSTICS = "acoustics", "Acoustics"
        VOICE_OBSERVATIONS = "voice_observations", "Voice observations"
        VOICE_CANDIDATES = "voice_candidates", "Voice candidates"
        SEMANTIC_ANALYSIS = "semantic_analysis", "Semantic analysis"
        IMAGE_DESCRIPTION = "image_description", "Image description"
        TRANSCRIPT = "transcript", "Transcript"
        VIDEO_DESCRIPTION = "video_description", "Video description"
        VALIDATION_WARNING = "validation_warning", "Validation warning"

    class EvidenceTier(models.TextChoices):
        CANONICAL = "canonical", "Canonical/operator fact"
        DETERMINISTIC = "deterministic", "Deterministic extraction"
        MODEL_GENERATED = "model_generated", "Model-generated hypothesis"
        OPERATOR_CONFIRMED = "operator_confirmed", "Operator-confirmed assertion"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage = models.ForeignKey(
        ProcessingStage,
        on_delete=models.PROTECT,
        related_name="results",
    )
    result_kind = models.CharField(max_length=48, choices=Kind.choices)
    evidence_tier = models.CharField(max_length=24, choices=EvidenceTier.choices)
    schema_version = models.PositiveSmallIntegerField(default=1)
    text = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    uncertainty = models.TextField(blank=True)
    processor_name = models.CharField(max_length=128)
    processor_version = models.CharField(max_length=64)
    prompt_template_version = models.CharField(max_length=64, blank=True)
    input_hashes = models.JSONField(default=dict, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    remote_request_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(confidence__isnull=True)
                | (Q(confidence__gte=0) & Q(confidence__lte=1)),
                name="processing_result_confidence_range",
            )
        ]
        indexes = [
            models.Index(
                fields=["stage", "result_kind"],
                name="processing_result_stage_idx",
            ),
            models.Index(
                fields=["result_kind", "created_at"],
                name="processing_result_kind_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_result_kind_display()} from {self.stage}"


class ArtifactPublication(models.Model):
    """A crash-recovery journal for one attempt's derived-file publication."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage = models.ForeignKey(
        ProcessingStage,
        on_delete=models.CASCADE,
        related_name="artifact_publications",
    )
    attempt = models.PositiveSmallIntegerField()
    file_name = models.CharField(max_length=500)
    temporary_name = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64, validators=[validate_sha256])
    byte_size = models.PositiveBigIntegerField()
    source_device = models.PositiveBigIntegerField()
    source_inode = models.PositiveBigIntegerField()
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["published_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["stage", "attempt", "file_name"],
                name="processing_unique_artifact_publication",
            ),
            models.CheckConstraint(
                condition=Q(attempt__gte=1),
                name="processing_artifact_publication_attempt",
            ),
        ]
        indexes = [
            models.Index(
                fields=["stage", "attempt"],
                name="processing_artifact_pub_idx",
            )
        ]

    def __str__(self) -> str:
        return f"Artifact publication {self.pk} for {self.stage}"


class DerivedArtifact(ImmutableEvidenceModel):
    """A separately stored derivative traceable to its source stage and item."""

    class Role(models.TextChoices):
        NORMALIZED_MEDIA = "normalized_media", "Normalized media"
        KEYFRAME = "keyframe", "Keyframe"
        CONTACT_SHEET = "contact_sheet", "Contact sheet"
        AUDIO_STREAM = "audio_stream", "Audio stream"
        AUDIO_CHUNK = "audio_chunk", "Audio chunk"
        SUBTITLE = "subtitle", "Subtitle"
        EMBEDDED_ARTWORK = "embedded_artwork", "Embedded artwork"
        OCR_LAYOUT = "ocr_layout", "OCR layout"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage = models.ForeignKey(
        ProcessingStage,
        on_delete=models.PROTECT,
        related_name="artifacts",
    )
    catalog_item = models.ForeignKey(
        CatalogItem,
        on_delete=models.PROTECT,
        related_name="derived_artifacts",
    )
    file = models.FileField(upload_to=derived_artifact_upload_to, max_length=500)
    role = models.CharField(max_length=32, choices=Role.choices)
    mime_type = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64, validators=[validate_sha256])
    byte_size = models.PositiveBigIntegerField()
    stream_index = models.PositiveIntegerField(null=True, blank=True)
    frame_index = models.PositiveBigIntegerField(null=True, blank=True)
    source_start_seconds = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    source_end_seconds = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(source_start_seconds__isnull=True)
                | Q(source_start_seconds__gte=0),
                name="processing_artifact_start_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(source_end_seconds__isnull=True)
                | Q(source_end_seconds__gte=0),
                name="processing_artifact_end_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(source_start_seconds__isnull=True)
                | Q(source_end_seconds__isnull=True)
                | Q(source_end_seconds__gte=F("source_start_seconds")),
                name="processing_artifact_ordered_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=["catalog_item", "role"],
                name="processing_artifact_item_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_role_display()} artifact {self.pk}"

    def clean(self) -> None:
        super().clean()
        if self.stage_id and self.catalog_item_id:
            run_item_id = self.stage.run.catalog_item_id
            if run_item_id != self.catalog_item_id:
                raise ValidationError(
                    {"catalog_item": "Artifact item must match its processing run."}
                )

    @property
    def basename(self) -> str:
        return PurePath(self.file.name).name


class TimestampClaim(ImmutableEvidenceModel):
    """A raw and optionally normalized timestamp extracted from evidence."""

    class Precision(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        YEAR = "year", "Year"
        MONTH = "month", "Month"
        DAY = "day", "Day"
        MINUTE = "minute", "Minute"
        SECOND = "second", "Second"
        SUBSECOND = "subsecond", "Subsecond"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage = models.ForeignKey(
        ProcessingStage,
        on_delete=models.PROTECT,
        related_name="timestamp_claims",
    )
    catalog_item = models.ForeignKey(
        CatalogItem,
        on_delete=models.PROTECT,
        related_name="timestamp_claims",
    )
    namespace = models.CharField(max_length=128)
    tag = models.CharField(max_length=255)
    raw_value = models.TextField()
    precision = models.CharField(
        max_length=16,
        choices=Precision.choices,
        default=Precision.UNKNOWN,
    )
    parsed_value = models.DateTimeField(
        null=True,
        blank=True,
        validators=[validate_timezone_aware],
    )
    timezone_evidence = models.TextField(blank=True)
    confidence = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    conflicts = models.JSONField(default=list, blank=True)
    provenance = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(confidence__isnull=True)
                | (Q(confidence__gte=0) & Q(confidence__lte=1)),
                name="processing_timestamp_confidence_range",
            )
        ]
        indexes = [
            models.Index(
                fields=["catalog_item", "created_at"],
                name="processing_timestamp_item_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.namespace}:{self.tag} timestamp claim"

    def clean(self) -> None:
        super().clean()
        if self.stage_id and self.catalog_item_id:
            run_item_id = self.stage.run.catalog_item_id
            if run_item_id != self.catalog_item_id:
                raise ValidationError(
                    {"catalog_item": "Timestamp item must match its processing run."}
                )


class TimestampDecision(ImmutableEvidenceModel):
    """An auditable operator promotion of a timestamp claim."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    claim = models.ForeignKey(
        TimestampClaim,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    catalog_item = models.ForeignKey(
        CatalogItem,
        on_delete=models.PROTECT,
        related_name="timestamp_decisions",
    )
    previous_occurred_at = models.DateTimeField(
        null=True,
        blank=True,
        validators=[validate_timezone_aware],
    )
    timezone_name = models.CharField(max_length=64, blank=True)
    fold = models.PositiveSmallIntegerField(null=True, blank=True)
    resulting_occurred_at = models.DateTimeField(
        validators=[validate_timezone_aware],
    )
    operator_note = models.TextField(blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["decided_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(fold__isnull=True) | Q(fold__in=(0, 1)),
                name="processing_timestamp_fold_valid",
            )
        ]

    def __str__(self) -> str:
        return f"Timestamp decision {self.pk}"

    def clean(self) -> None:
        super().clean()
        if self.claim_id and self.catalog_item_id:
            if self.claim.catalog_item_id != self.catalog_item_id:
                raise ValidationError(
                    {"catalog_item": "Decision item must match its timestamp claim."}
                )


class RemoteConsent(ImmutableEvidenceModel):
    """A fingerprint-bound record of one item-specific remote transfer consent."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(
        ProcessingRun,
        on_delete=models.PROTECT,
        related_name="remote_consents",
    )
    catalog_item = models.ForeignKey(
        CatalogItem,
        on_delete=models.PROTECT,
        related_name="remote_consents",
    )
    notice_version = models.CharField(max_length=64)
    source_fingerprint = models.CharField(
        max_length=64,
        validators=[validate_sha256],
    )
    payload_manifest = models.JSONField()
    payload_manifest_hash = models.CharField(
        max_length=64,
        validators=[validate_sha256],
    )
    recipients = models.JSONField()
    model_ids = models.JSONField()
    prompt_versions = models.JSONField(default=dict, blank=True)
    planned_request_count = models.PositiveIntegerField()
    consented_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-consented_at", "id"]
        indexes = [
            models.Index(
                fields=["run", "-consented_at"],
                name="processing_consent_run_idx",
            )
        ]

    def __str__(self) -> str:
        return f"Remote consent {self.pk} for run {self.run_id}"

    def clean(self) -> None:
        super().clean()
        if self.run_id and self.catalog_item_id:
            if self.run.catalog_item_id != self.catalog_item_id:
                raise ValidationError(
                    {"catalog_item": "Consent item must match its processing run."}
                )
        if self.run_id and self.source_fingerprint:
            if self.run.source_fingerprint != self.source_fingerprint:
                raise ValidationError(
                    {"source_fingerprint": "Consent must match the run fingerprint."}
                )

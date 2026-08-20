"""Local, closed-gallery identity evidence and operator decisions."""

import math
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import (
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.db import models
from django.utils import timezone

from pandect.catalog.models import (
    CatalogItem,
    ValidatedModel,
    validate_non_whitespace,
    validate_timezone_aware,
)

validate_sha256 = RegexValidator(
    regex=r"\A[0-9a-f]{64}\Z",
    message="Enter a lowercase hexadecimal SHA-256 digest.",
    code="invalid_sha256",
)


class AppendOnlyIdentityQuerySet(models.QuerySet):
    """Reject bulk mutation of immutable identity evidence and history."""

    def update(self, **kwargs):
        raise TypeError(f"{self.model.__name__} records are append-only.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise TypeError(f"{self.model.__name__} records are append-only.")

    def delete(self):
        raise TypeError(f"{self.model.__name__} records are append-only.")


class AppendOnlyIdentityModel(ValidatedModel):
    """Create-only base for identity observations and decision history."""

    objects = AppendOnlyIdentityQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError(f"{type(self).__name__} records are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError(f"{type(self).__name__} records are append-only.")


class Subject(ValidatedModel):
    """A locally meaningful person identity resolved by the operator."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(
        max_length=255,
        validators=[validate_non_whitespace],
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label", "id"]

    def __str__(self) -> str:
        return self.label


class IdentityObservation(AppendOnlyIdentityModel):
    """A local face, voice, or writing feature traced to one catalogue item."""

    class Modality(models.TextChoices):
        FACE = "face", "Face"
        VOICE = "voice", "Voice"
        WRITING = "writing", "Writing"

    class QualityStatus(models.TextChoices):
        UNASSESSED = "unassessed", "Unassessed"
        APPROVED = "approved", "Approved for matching"
        REJECTED = "rejected", "Rejected for matching"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    catalog_item = models.ForeignKey(
        CatalogItem,
        on_delete=models.CASCADE,
        related_name="identity_observations",
    )
    modality = models.CharField(max_length=16, choices=Modality.choices)
    unit_key = models.CharField(
        max_length=255,
        validators=[validate_non_whitespace],
        help_text="Stable location key within the source item.",
    )
    source_location = models.JSONField(default=dict)
    source_sha256 = models.CharField(max_length=64, validators=[validate_sha256])

    # Embeddings are intentionally opaque to forms, templates, and admin-like APIs.
    embedding = models.BinaryField(editable=False)
    embedding_dimension = models.PositiveIntegerField()
    feature_space = models.CharField(
        max_length=255,
        validators=[validate_non_whitespace],
        help_text="Exact embedding/preprocessing compatibility identifier.",
    )

    processor_name = models.CharField(
        max_length=128,
        validators=[validate_non_whitespace],
    )
    processor_version = models.CharField(
        max_length=64,
        validators=[validate_non_whitespace],
    )
    model_identifier = models.CharField(max_length=255, blank=True)
    model_version = models.CharField(max_length=128, blank=True)
    model_sha256 = models.CharField(
        max_length=64,
        blank=True,
        validators=[validate_sha256],
    )
    preprocessing_version = models.CharField(
        max_length=128,
        validators=[validate_non_whitespace],
    )
    provenance = models.JSONField(default=dict)

    quality_status = models.CharField(
        max_length=16,
        choices=QualityStatus.choices,
        default=QualityStatus.UNASSESSED,
    )
    quality_score = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    quality_details = models.JSONField(default=dict)
    sample_duration_ms = models.PositiveBigIntegerField(null=True, blank=True)
    sample_word_count = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["catalog_item", "modality"],
                name="identity_item_modality_idx",
            ),
            models.Index(
                fields=["modality", "feature_space"],
                name="identity_modality_space_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(embedding_dimension__gt=0),
                name="identity_observation_dimension_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(quality_score__isnull=True)
                | models.Q(quality_score__gte=0.0, quality_score__lte=1.0),
                name="identity_observation_quality_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        modality="face",
                        sample_duration_ms__isnull=True,
                        sample_word_count__isnull=True,
                    )
                    | models.Q(
                        modality="voice",
                        sample_duration_ms__gt=0,
                        sample_word_count__isnull=True,
                    )
                    | models.Q(
                        modality="writing",
                        sample_duration_ms__isnull=True,
                        sample_word_count__gt=0,
                    )
                ),
                name="identity_observation_modality_sample",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_modality_display()} observation {self.pk}"

    def clean(self) -> None:
        super().clean()
        errors: dict[str, str] = {}
        if not isinstance(self.source_location, dict):
            errors["source_location"] = "Source location must be a JSON object."
        if not isinstance(self.provenance, dict):
            errors["provenance"] = "Provenance must be a JSON object."
        if not isinstance(self.quality_details, dict):
            errors["quality_details"] = "Quality details must be a JSON object."

        embedding = bytes(self.embedding or b"")
        if self.embedding_dimension and len(embedding) != self.embedding_dimension * 4:
            errors["embedding"] = (
                "Embedding must contain one 32-bit float per declared dimension."
            )
        elif embedding:
            from pandect.identities.services import decode_embedding

            try:
                decode_embedding(embedding, self.embedding_dimension)
            except ValueError as error:
                errors["embedding"] = str(error)

        if isinstance(self.source_location, dict):
            location_error = self._validate_source_location()
            if location_error:
                errors["source_location"] = location_error

        if errors:
            raise ValidationError(errors)

    def _validate_source_location(self) -> str | None:
        location = self.source_location
        if self.modality == self.Modality.FACE:
            bbox = location.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                return "A face location requires bbox [x, y, width, height]."
            if not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in bbox
            ):
                return "Face bounding-box coordinates must be finite numbers."
            if bbox[0] < 0 or bbox[1] < 0 or bbox[2] <= 0 or bbox[3] <= 0:
                return "Face bounding-box coordinates and dimensions are invalid."
            frame_index = location.get("frame_index")
            if frame_index is not None and (
                not isinstance(frame_index, int)
                or isinstance(frame_index, bool)
                or frame_index < 0
            ):
                return "Face frame_index must be a non-negative integer."
            timestamp_ms = location.get("timestamp_ms")
            if timestamp_ms is not None and (
                not isinstance(timestamp_ms, (int, float))
                or isinstance(timestamp_ms, bool)
                or not math.isfinite(timestamp_ms)
                or timestamp_ms < 0
            ):
                return "Face timestamp_ms must be a non-negative finite number."
            return None

        if self.modality == self.Modality.VOICE:
            start = location.get("start_ms")
            end = location.get("end_ms")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
            ):
                return "A voice location requires integer start_ms < end_ms."
            if self.sample_duration_ms != end - start:
                return "Voice sample duration must equal its source interval."
            return None

        if self.modality == self.Modality.WRITING:
            start = location.get("start")
            end = location.get("end")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
            ):
                return "A writing location requires integer start < end offsets."
            return None

        return "Select a supported identity modality."


class SubjectReference(ValidatedModel):
    """An operator-selected observation assigned to one gallery subject."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="references",
    )
    observation = models.OneToOneField(
        IdentityObservation,
        on_delete=models.CASCADE,
        related_name="subject_reference",
    )
    notes = models.TextField(blank=True)
    selected_at = models.DateTimeField(
        default=timezone.now,
        validators=[validate_timezone_aware],
    )

    class Meta:
        ordering = ["selected_at", "id"]

    def __str__(self) -> str:
        return f"{self.subject}: {self.observation.get_modality_display()} reference"

    def clean(self) -> None:
        super().clean()
        if not self.observation_id:
            return
        if (
            self.observation.quality_status
            != IdentityObservation.QualityStatus.APPROVED
        ):
            raise ValidationError(
                {"observation": "Only quality-approved observations may be references."}
            )
        from pandect.identities.services import observation_is_gallery_eligible

        if not observation_is_gallery_eligible(self.observation):
            raise ValidationError(
                {
                    "observation": (
                        "Only aggregate video face tracks may be selected as "
                        "subject references."
                    )
                }
            )


class SubjectReferenceDecision(AppendOnlyIdentityModel):
    """Append-only history of every operator reference-gallery decision."""

    class Action(models.TextChoices):
        SELECTED = "selected", "Selected as reference"
        UPDATED = "updated", "Reference updated"
        REMOVED = "removed", "Reference removed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference_id = models.UUIDField(
        help_text="Stable identifier of the reference row at decision time."
    )
    observation = models.ForeignKey(
        IdentityObservation,
        on_delete=models.PROTECT,
        related_name="reference_decision_history",
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.PROTECT,
        related_name="reference_decision_history",
    )
    previous_subject = models.ForeignKey(
        Subject,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reference_previous_decision_history",
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    subject_label = models.CharField(
        max_length=255,
        validators=[validate_non_whitespace],
    )
    previous_subject_label = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    previous_notes = models.TextField(blank=True)
    decided_at = models.DateTimeField(
        default=timezone.now,
        validators=[validate_timezone_aware],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["decided_at", "created_at", "id"]
        indexes = [
            models.Index(
                fields=["observation", "-decided_at"],
                name="identity_ref_decision_obs_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} for {self.observation}"

    def clean(self) -> None:
        super().clean()
        errors = {}
        if self.action == self.Action.UPDATED:
            if not self.previous_subject_id:
                errors["previous_subject"] = (
                    "An updated reference must retain its previous subject."
                )
            if not (self.previous_subject_label or "").strip():
                errors["previous_subject_label"] = (
                    "An updated reference must retain its previous subject label."
                )
        elif self.previous_subject_id or self.previous_subject_label:
            errors["previous_subject"] = (
                "Only an updated reference records a previous subject."
            )
        if errors:
            raise ValidationError(errors)


class IdentityGalleryRematchJob(models.Model):
    """Durable cursor for bounded global rematching after a gallery change."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.OneToOneField(
        SubjectReferenceDecision,
        on_delete=models.PROTECT,
        related_name="rematch_job",
    )
    modality = models.CharField(
        max_length=16,
        choices=IdentityObservation.Modality.choices,
    )
    upper_item_id = models.UUIDField(null=True, blank=True)
    cursor_item_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    runs_created = models.PositiveIntegerField(default=0)
    failure_count = models.PositiveSmallIntegerField(default=0)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        validators=[validate_timezone_aware],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["status", "lease_expires_at"],
                name="identity_gallery_job_lease_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(status="succeeded", completed_at__isnull=False)
                    | models.Q(
                        status__in=("pending", "running"),
                        completed_at__isnull=True,
                    )
                ),
                name="identity_gallery_job_completion",
            )
        ]

    def __str__(self) -> str:
        return f"Gallery rematch {self.pk} ({self.status})"

    def clean(self) -> None:
        super().clean()
        if self.decision_id and self.modality != self.decision.observation.modality:
            raise ValidationError(
                {"modality": "Rematch modality must match the reference observation."}
            )


class IdentityCandidate(ValidatedModel):
    """One versioned, closed-gallery subject candidate for an observation."""

    class Decision(models.TextChoices):
        SUGGESTED = "suggested", "Suggested"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate_set_id = models.UUIDField()
    producing_run = models.ForeignKey(
        "processing.ProcessingRun",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="identity_candidates",
        help_text="Durable processing run that produced this candidate set.",
    )
    producing_stage = models.ForeignKey(
        "processing.ProcessingStage",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.PROTECT,
        related_name="identity_candidates",
        help_text="Durable processing stage that produced this candidate set.",
    )
    observation = models.ForeignKey(
        IdentityObservation,
        on_delete=models.CASCADE,
        related_name="identity_candidates",
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="identity_candidates",
    )
    modality = models.CharField(
        max_length=16,
        choices=IdentityObservation.Modality.choices,
    )
    feature_space = models.CharField(max_length=255)
    gallery_fingerprint = models.CharField(
        max_length=64,
        validators=[validate_sha256],
    )
    rank = models.PositiveSmallIntegerField()
    similarity_score = models.FloatField()
    reference_count = models.PositiveIntegerField()
    calibration_revision = models.CharField(
        max_length=128,
        validators=[validate_non_whitespace],
    )
    decision = models.CharField(
        max_length=16,
        choices=Decision.choices,
        default=Decision.SUGGESTED,
    )
    decided_at = models.DateTimeField(
        null=True,
        blank=True,
        validators=[validate_timezone_aware],
    )
    decision_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["candidate_set_id", "rank", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["candidate_set_id", "observation", "subject"],
                name="identity_candidate_subject_once",
            ),
            models.UniqueConstraint(
                fields=["candidate_set_id", "observation", "rank"],
                name="identity_candidate_rank_once",
            ),
            models.CheckConstraint(
                condition=models.Q(rank__gte=1, rank__lte=5),
                name="identity_candidate_rank_range",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    similarity_score__gte=-1.0,
                    similarity_score__lte=1.0,
                ),
                name="identity_candidate_similarity_range",
            ),
            models.CheckConstraint(
                condition=models.Q(reference_count__gt=0),
                name="identity_candidate_reference_count_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        producing_run__isnull=True,
                        producing_stage__isnull=True,
                    )
                    | models.Q(
                        producing_run__isnull=False,
                        producing_stage__isnull=False,
                    )
                ),
                name="identity_candidate_provenance_pair",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(decision="suggested", decided_at__isnull=True)
                    | models.Q(
                        decision__in=["confirmed", "rejected"],
                        decided_at__isnull=False,
                    )
                ),
                name="identity_candidate_decision_timestamp",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject} candidate for {self.observation}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            immutable_fields = (
                "candidate_set_id",
                "producing_run_id",
                "producing_stage_id",
                "observation_id",
                "subject_id",
                "modality",
                "feature_space",
                "gallery_fingerprint",
                "rank",
                "similarity_score",
                "reference_count",
                "calibration_revision",
            )
            original = type(self).objects.values(*immutable_fields).get(pk=self.pk)
            if any(
                getattr(self, field_name) != original[field_name]
                for field_name in immutable_fields
            ):
                raise TypeError(
                    "IdentityCandidate evidence and provenance are immutable."
                )
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        if not self.observation_id:
            return
        errors = {}
        if self.modality != self.observation.modality:
            errors["modality"] = "Candidate modality must match the observation."
        if self.feature_space != self.observation.feature_space:
            errors["feature_space"] = (
                "Candidate feature space must match the observation."
            )
        if bool(self.producing_run_id) != bool(self.producing_stage_id):
            errors["producing_stage"] = (
                "Candidate run and stage provenance must be recorded together."
            )
        elif self.producing_stage_id:
            if self.producing_stage.run_id != self.producing_run_id:
                errors["producing_stage"] = (
                    "Candidate stage must belong to its producing run."
                )
            elif self.producing_run.catalog_item_id != self.observation.catalog_item_id:
                errors["producing_run"] = (
                    "Candidate run must belong to the observation's catalogue item."
                )
        if errors:
            raise ValidationError(errors)


class IdentityCandidateDecision(AppendOnlyIdentityModel):
    """Append-only history of every candidate confirmation or rejection."""

    class Origin(models.TextChoices):
        OPERATOR = "operator", "Operator"
        SUPERSESSION = "supersession", "Superseded by another confirmation"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        IdentityCandidate,
        on_delete=models.PROTECT,
        related_name="decision_history",
    )
    decision = models.CharField(
        max_length=16,
        choices=(
            (IdentityCandidate.Decision.CONFIRMED, "Confirmed"),
            (IdentityCandidate.Decision.REJECTED, "Rejected"),
        ),
    )
    origin = models.CharField(
        max_length=16,
        choices=Origin.choices,
        default=Origin.OPERATOR,
    )
    note = models.TextField(blank=True)
    decided_at = models.DateTimeField(
        default=timezone.now,
        validators=[validate_timezone_aware],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["decided_at", "created_at", "id"]
        indexes = [
            models.Index(
                fields=["candidate", "-decided_at"],
                name="identity_decision_cand_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_decision_display()} {self.candidate}"


class IdentityAssertion(ValidatedModel):
    """Historical record of an operator-confirmed subject association."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        RETRACTED = "retracted", "Retracted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey(
        IdentityCandidate,
        on_delete=models.CASCADE,
        related_name="assertions",
    )
    observation = models.ForeignKey(
        IdentityObservation,
        on_delete=models.CASCADE,
        related_name="identity_assertions",
    )
    subject = models.ForeignKey(
        Subject,
        on_delete=models.CASCADE,
        related_name="identity_assertions",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    asserted_at = models.DateTimeField(
        default=timezone.now,
        validators=[validate_timezone_aware],
    )
    retracted_at = models.DateTimeField(
        null=True,
        blank=True,
        validators=[validate_timezone_aware],
    )
    note = models.TextField(blank=True)
    retraction_note = models.TextField(blank=True)

    class Meta:
        ordering = ["asserted_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["observation"],
                condition=models.Q(status="active"),
                name="identity_one_active_assertion",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="active", retracted_at__isnull=True)
                    | models.Q(status="retracted", retracted_at__isnull=False)
                ),
                name="identity_assertion_status_timestamp",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject} asserted for {self.observation} ({self.status})"

    def clean(self) -> None:
        super().clean()
        if not self.candidate_id:
            return
        errors = {}
        if self.observation_id != self.candidate.observation_id:
            errors["observation"] = "Assertion observation must match the candidate."
        if self.subject_id != self.candidate.subject_id:
            errors["subject"] = "Assertion subject must match the candidate."
        if (
            self.status == self.Status.ACTIVE
            and self.candidate.decision != IdentityCandidate.Decision.CONFIRMED
        ):
            errors["candidate"] = "An active assertion requires a confirmed candidate."
        elif self.candidate.decision == IdentityCandidate.Decision.SUGGESTED:
            errors["candidate"] = "An assertion requires an operator-decided candidate."
        if (
            self.asserted_at
            and self.retracted_at
            and timezone.is_aware(self.asserted_at)
            and timezone.is_aware(self.retracted_at)
            and self.retracted_at < self.asserted_at
        ):
            errors["retracted_at"] = (
                "An assertion cannot be retracted before it exists."
            )
        if errors:
            raise ValidationError(errors)

"""Retained investigation processing history, independent of disposable queue rows."""

from uuid import uuid4

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models

from core.storage import original_upload_path


class CaseAnalysisBatch(models.Model):
    """Durable cursor for preparing bounded, case-wide local checks."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Preparing checks"
        COMPLETE = "complete", "Checks queued"
        FAILED = "failed", "Preparation stopped"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    submission_id = models.UUIDField(null=True, blank=True, editable=False)
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    source_kind = models.CharField(max_length=32, blank=True)
    source_cursor = models.UUIDField(null=True, blank=True)
    processed_sources = models.PositiveIntegerField(default=0)
    unavailable_sources = models.PositiveIntegerField(default=0)
    queued_runs = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    preparation_errors = models.JSONField(default=list)
    scope = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=("case", "requested_by", "submission_id"),
                name="analysis_batch_submission_scope",
            ),
        ]

    def __str__(self):
        return f"Case analysis {self.pk}"


class AnalysisRun(models.Model):
    class Stage(models.TextChoices):
        SOURCES = "sources", "Checking sources"
        ANALYSIS = "analysis", "Running analysis"
        PUBLICATION = "publication", "Saving results"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        PARTIAL = "partial", "Partial"
        EMPTY = "empty", "No findings"
        INSUFFICIENT = "insufficient", "Insufficient evidence"
        UNSUPPORTED = "unsupported", "Unsupported input"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    operation = models.CharField(max_length=64)
    method_version = models.CharField(max_length=64)
    parameters = models.JSONField(default=dict)
    inputs = models.JSONField(default=list)
    equivalence_key = models.CharField(
        max_length=64, default="", db_index=True, editable=False
    )
    batch = models.ForeignKey(
        CaseAnalysisBatch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="runs",
    )
    submission_id = models.UUIDField(default=uuid4, editable=False)
    previous_run = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL
    )
    external_processing = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    # Applies while running. Older workers can omit the field when creating runs.
    stage = models.CharField(
        max_length=16, choices=Stage, default=Stage.ANALYSIS, db_default=Stage.ANALYSIS
    )
    progress = models.PositiveSmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "id"]
        indexes = [
            GinIndex(
                fields=["inputs"],
                opclasses=["jsonb_path_ops"],
                name="analysis_inputs_gin",
            ),
            GinIndex(
                fields=["parameters"],
                opclasses=["jsonb_path_ops"],
                name="analysis_parameters_gin",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("case", "requested_by", "submission_id"),
                name="analysis_run_submission_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(progress__lte=100), name="analysis_progress_range"
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__lte=3), name="analysis_attempt_bound"
            ),
        ]

    def __str__(self):
        return f"Processing {self.pk}"

    def save(self, *args, **kwargs):
        if self.previous_run_id and self.previous_run.case_id != self.case_id:
            raise ValidationError("Earlier processing must belong to the same case.")
        if self.batch_id and self.batch.case_id != self.case_id:
            raise ValidationError("The analysis batch must belong to the same case.")
        if not self._state.adding:
            prior = type(self).objects.get(pk=self.pk)
            immutable = (
                "case_id",
                "created_at",
                "requested_by_id",
                "operation",
                "method_version",
                "parameters",
                "inputs",
                "submission_id",
                "previous_run_id",
                "external_processing",
                "equivalence_key",
                "batch_id",
            )
            if any(getattr(self, name) != getattr(prior, name) for name in immutable):
                raise ValidationError(
                    "Inputs and attribution are retained. Start another run."
                )
        if self._state.adding:
            from core.analysis.projection import equivalence_key

            self.equivalence_key = equivalence_key(self)
            kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    @property
    def progress_label(self):
        if self.status == self.Status.RUNNING:
            return self.get_stage_display()
        return self.get_status_display()


class AnalysisResult(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    run = models.OneToOneField(
        AnalysisRun, on_delete=models.CASCADE, related_name="result"
    )
    data = models.JSONField(default=dict)
    provenance = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Result {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Retained results cannot be replaced. Start another run."
            )
        self.clean()
        if self._state.adding:
            kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.run_id and self.run.case_id != self.case_id:
            raise ValidationError("The result must belong to its processing case.")


class AnalysisFinding(models.Model):
    """Searchable projection; the result item remains the authoritative record."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    result = models.ForeignKey(
        AnalysisResult, on_delete=models.CASCADE, related_name="findings"
    )
    item_index = models.PositiveIntegerField()
    label = models.TextField()
    preview = models.CharField(max_length=300, blank=True)
    search_text = models.TextField()
    weak = models.BooleanField(default=False)
    match_kind = models.CharField(max_length=16, blank=True)
    match_key = models.TextField(blank=True)
    match_digest = models.CharField(max_length=64, blank=True)
    match_version = models.CharField(max_length=32, blank=True)
    review_signal = models.CharField(max_length=32, blank=True)
    repeat_key = models.CharField(max_length=64, blank=True)
    hidden_from_catalogue = models.BooleanField(default=False, db_default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["result", "item_index"], name="analysis_finding_item_unique"
            )
        ]
        indexes = [models.Index(fields=["case", "match_kind", "match_digest"])]

    def __str__(self):
        return f"Finding {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Finding projections are retained with their result.")
        if self.result.case_id != self.case_id:
            raise ValidationError("Finding and result must belong to the same case.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)


class AnalysisFindingSource(models.Model):
    """Only original identities actually cited by an individual finding."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    finding = models.ForeignKey(
        AnalysisFinding, on_delete=models.CASCADE, related_name="sources"
    )
    source_id = models.UUIDField()
    kind = models.CharField(max_length=32)
    evidence_id = models.UUIDField()
    attachment_id = models.UUIDField(null=True, blank=True)
    source_sha256 = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["finding", "source_id"], name="analysis_finding_source_unique"
            )
        ]
        indexes = [models.Index(fields=["case", "kind", "evidence_id"])]

    def __str__(self):
        return f"Finding source {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Finding sources are retained with their result.")
        if self.finding.case_id != self.case_id:
            raise ValidationError("Finding source and finding must share a case.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)


class AnalysisReview(models.Model):
    """Retain legacy reasons and the decisions pinned by geographic runs."""

    class Decision(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        UNRESOLVED = "unresolved", "Unresolved"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    result = models.ForeignKey(
        AnalysisResult, on_delete=models.CASCADE, related_name="reviews"
    )
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decision = models.CharField(max_length=16, choices=Decision)
    comment = models.TextField(blank=True, max_length=4000)
    item_index = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]

    def __str__(self):
        return f"Review {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Reviews are retained. Add a further review.")
        self.full_clean()
        if self._state.adding:
            kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.result_id and self.result.case_id != self.case_id:
            raise ValidationError("Review and result must belong to the same case.")
        if self.item_index is not None and self.item_index >= len(
            self.result.data.get("items", [])
        ):
            raise ValidationError("Choose an available finding to review.")


class GatewayRequest(models.Model):
    class Operation(models.TextChoices):
        ANALYSIS = "analysis", "Analysis"
        SEARCH_INDEX = "search_index", "Meaning search indexing"
        SEARCH_QUERY = "search_query", "Meaning search query"
        EVIDENCE_CHAT = "evidence_chat", "Evidence chat"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    run = models.ForeignKey(
        AnalysisRun,
        on_delete=models.CASCADE,
        related_name="gateway_requests",
        blank=True,
        null=True,
    )
    chat_turn = models.ForeignKey(
        "core.ChatTurn",
        on_delete=models.CASCADE,
        related_name="gateway_requests",
        blank=True,
        null=True,
    )
    operation = models.CharField(
        max_length=16, choices=Operation, default=Operation.ANALYSIS
    )
    model = models.CharField(max_length=255)
    status = models.CharField(max_length=32, default="pending")
    request_bytes = models.PositiveIntegerField()
    response_bytes = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(null=True)
    output_tokens = models.PositiveIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        operation="analysis", run__isnull=False, chat_turn__isnull=True
                    )
                    | models.Q(
                        operation="evidence_chat",
                        run__isnull=True,
                        chat_turn__isnull=False,
                    )
                    | models.Q(
                        operation__in=["search_index", "search_query"],
                        run__isnull=True,
                        chat_turn__isnull=True,
                    )
                ),
                name="gateway_usage_operation_run",
            )
        ]

    def __str__(self):
        return f"Gateway request {self.pk}"

    def save(self, *args, **kwargs):
        if self.run_id is not None and self.run.case_id != self.case_id:
            raise ValidationError("Gateway usage must belong to the run's case.")
        if self.chat_turn_id is not None and self.chat_turn.case_id != self.case_id:
            raise ValidationError("Gateway usage must belong to the chat's case.")
        if self._state.adding:
            kwargs["force_insert"] = True
        return super().save(*args, **kwargs)


class ExtractedPassage(models.Model):
    """Concrete lexical-search projection of retained extraction output."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    result = models.ForeignKey(
        AnalysisResult, on_delete=models.CASCADE, related_name="passages"
    )
    kind = models.CharField(max_length=32)
    evidence_id = models.UUIDField()
    evidence_sha256 = models.CharField(max_length=64)
    attachment_id = models.UUIDField(null=True, blank=True)
    text = models.TextField()
    item_index = models.PositiveIntegerField(default=0)
    anchors = models.JSONField(default=list)

    class Meta:
        indexes = [models.Index(fields=["case", "kind", "evidence_id"])]

    def __str__(self):
        return f"Extracted passage {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Extracted passages are retained with their result.")
        if self.result.case_id != self.case_id:
            raise ValidationError("Passage and result must belong to the same case.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)


class AnalysisArtifact(models.Model):
    """Private previews and geographic reference responses retained with results."""

    class Kind(models.TextChoices):
        IMAGE_PREVIEW = "image_preview", "Image preview"
        GEOGRAPHIC_REFERENCE = "geographic_reference", "Geographic reference"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    result = models.ForeignKey(
        AnalysisResult, on_delete=models.CASCADE, related_name="artifacts"
    )
    source_id = models.UUIDField()
    kind = models.CharField(
        max_length=32, choices=Kind.choices, default=Kind.IMAGE_PREVIEW
    )
    file = models.FileField(upload_to=original_upload_path)
    sha256 = models.CharField(max_length=64)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="image_preview",
                        width__isnull=False,
                        height__isnull=False,
                        width__gte=1,
                        width__lte=4096,
                        height__gte=1,
                        height__lte=4096,
                    )
                    | models.Q(
                        kind="geographic_reference",
                        width__isnull=True,
                        height__isnull=True,
                    )
                ),
                name="analysis_artifact_dimensions",
            )
        ]

    def __str__(self):
        return f"Analysis artifact {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Derived artifacts are retained with their result.")
        if self.result.case_id != self.case_id or str(self.source_id) not in {
            x["id"] for x in self.result.run.inputs
        }:
            raise ValidationError(
                "The artifact must refer to its result's case and source."
            )
        if self.kind == self.Kind.IMAGE_PREVIEW:
            if not all(
                type(value) is int and 1 <= value <= 4096
                for value in (self.width, self.height)
            ):
                raise ValidationError("Preview dimensions exceed the supported range.")
        elif self.kind == self.Kind.GEOGRAPHIC_REFERENCE:
            if self.width is not None or self.height is not None:
                raise ValidationError("Geographic references have no image dimensions.")
        else:
            raise ValidationError("Choose a supported artifact kind.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

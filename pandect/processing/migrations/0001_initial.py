import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

import pandect.catalog.models
import pandect.processing.models

SHA256_VALIDATOR = django.core.validators.RegexValidator(
    code="invalid_sha256",
    message="Enter a lowercase hexadecimal SHA-256 digest.",
    regex=r"\A[0-9a-f]{64}\Z",
)


class Migration(migrations.Migration):
    initial = True

    dependencies = [("catalog", "0002_catalogitem_audio_type")]

    operations = [
        migrations.CreateModel(
            name="ProcessingRun",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "trigger",
                    models.CharField(
                        choices=[
                            ("automatic", "Automatic"),
                            ("manual", "Manual"),
                            ("backfill", "Backfill"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "scope",
                    models.CharField(
                        choices=[("local", "Local"), ("remote", "Remote")],
                        default="local",
                        max_length=16,
                    ),
                ),
                (
                    "source_fingerprint",
                    models.CharField(max_length=64, validators=[SHA256_VALIDATOR]),
                ),
                ("source_size", models.PositiveBigIntegerField()),
                ("processor_version", models.CharField(default="1", max_length=64)),
                ("configuration", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("partial", "Partial"),
                            ("failed", "Failed"),
                            ("superseded", "Superseded"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "catalog_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="processing_runs",
                        to="catalog.catalogitem",
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "id"]},
        ),
        migrations.CreateModel(
            name="ProcessingStage",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("text_extraction", "Text extraction"),
                            ("writing_style", "Writing style"),
                            ("writing_identity", "Writing identity"),
                            ("image_metadata", "Image metadata"),
                            ("image_ocr", "Image OCR"),
                            ("image_qr", "Image QR extraction"),
                            ("image_visual", "Image visual analysis"),
                            ("face_analysis", "Face analysis"),
                            ("face_identity", "Face identity"),
                            ("video_metadata", "Video metadata"),
                            ("video_subtitles", "Video subtitles"),
                            ("video_keyframes", "Video keyframes"),
                            ("video_audio", "Video audio extraction"),
                            ("video_frame_analysis", "Video frame analysis"),
                            ("audio_metadata", "Audio metadata"),
                            ("audio_split", "Audio splitting"),
                            ("audio_acoustics", "Audio acoustic analysis"),
                            ("voice_analysis", "Voice analysis"),
                            ("voice_identity", "Voice identity"),
                            ("remote_semantic", "Remote semantic analysis"),
                            (
                                "remote_image_description",
                                "Remote image description",
                            ),
                            ("remote_transcription", "Remote transcription"),
                            (
                                "remote_video_description",
                                "Remote video description",
                            ),
                        ],
                        max_length=48,
                    ),
                ),
                ("unit_key", models.CharField(default="default", max_length=128)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                            ("superseded", "Superseded"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=2)),
                ("task_id", models.CharField(blank=True, max_length=64)),
                ("priority", models.SmallIntegerField(default=0)),
                ("processor_name", models.CharField(blank=True, max_length=128)),
                ("processor_version", models.CharField(default="1", max_length=64)),
                ("schema_version", models.PositiveSmallIntegerField(default=1)),
                ("failure_code", models.CharField(blank=True, max_length=64)),
                ("failure_detail", models.CharField(blank=True, max_length=512)),
                ("queued_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stages",
                        to="processing.processingrun",
                    ),
                ),
            ],
            options={"ordering": ["priority", "created_at", "id"]},
        ),
        migrations.CreateModel(
            name="ProcessingResult",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "result_kind",
                    models.CharField(
                        choices=[
                            ("message_text_profile", "Message text profile"),
                            ("text_extraction", "Text extraction"),
                            ("writing_style", "Writing style"),
                            ("authorship_candidates", "Authorship candidates"),
                            ("image_metadata", "Image metadata"),
                            ("ocr", "OCR"),
                            ("qr_codes", "QR codes"),
                            ("image_visual", "Image visual analysis"),
                            ("face_observations", "Face observations"),
                            ("face_candidates", "Face candidates"),
                            ("video_metadata", "Video metadata"),
                            ("subtitles", "Subtitles"),
                            ("keyframes", "Keyframes"),
                            ("audio_streams", "Audio streams"),
                            ("audio_metadata", "Audio metadata"),
                            ("audio_splits", "Audio splits"),
                            ("acoustics", "Acoustics"),
                            ("voice_observations", "Voice observations"),
                            ("voice_candidates", "Voice candidates"),
                            ("semantic_analysis", "Semantic analysis"),
                            ("image_description", "Image description"),
                            ("transcript", "Transcript"),
                            ("video_description", "Video description"),
                            ("validation_warning", "Validation warning"),
                        ],
                        max_length=48,
                    ),
                ),
                (
                    "evidence_tier",
                    models.CharField(
                        choices=[
                            ("canonical", "Canonical/operator fact"),
                            ("deterministic", "Deterministic extraction"),
                            ("model_generated", "Model-generated hypothesis"),
                            (
                                "operator_confirmed",
                                "Operator-confirmed assertion",
                            ),
                        ],
                        max_length=24,
                    ),
                ),
                ("schema_version", models.PositiveSmallIntegerField(default=1)),
                ("text", models.TextField(blank=True)),
                ("data", models.JSONField(blank=True, default=dict)),
                (
                    "confidence",
                    models.FloatField(
                        blank=True,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(1),
                        ],
                    ),
                ),
                ("uncertainty", models.TextField(blank=True)),
                ("processor_name", models.CharField(max_length=128)),
                ("processor_version", models.CharField(max_length=64)),
                (
                    "prompt_template_version",
                    models.CharField(blank=True, max_length=64),
                ),
                ("input_hashes", models.JSONField(blank=True, default=dict)),
                ("provenance", models.JSONField(blank=True, default=dict)),
                (
                    "remote_request_metadata",
                    models.JSONField(blank=True, default=dict),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "stage",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="results",
                        to="processing.processingstage",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="DerivedArtifact",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "file",
                    models.FileField(
                        max_length=500,
                        upload_to=pandect.processing.models.derived_artifact_upload_to,
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("normalized_media", "Normalized media"),
                            ("keyframe", "Keyframe"),
                            ("contact_sheet", "Contact sheet"),
                            ("audio_stream", "Audio stream"),
                            ("audio_chunk", "Audio chunk"),
                            ("subtitle", "Subtitle"),
                            ("embedded_artwork", "Embedded artwork"),
                            ("ocr_layout", "OCR layout"),
                            ("other", "Other"),
                        ],
                        max_length=32,
                    ),
                ),
                ("mime_type", models.CharField(max_length=255)),
                (
                    "sha256",
                    models.CharField(max_length=64, validators=[SHA256_VALIDATOR]),
                ),
                ("byte_size", models.PositiveBigIntegerField()),
                ("stream_index", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "frame_index",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                (
                    "source_start_seconds",
                    models.DecimalField(
                        blank=True,
                        decimal_places=6,
                        max_digits=20,
                        null=True,
                    ),
                ),
                (
                    "source_end_seconds",
                    models.DecimalField(
                        blank=True,
                        decimal_places=6,
                        max_digits=20,
                        null=True,
                    ),
                ),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "catalog_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="derived_artifacts",
                        to="catalog.catalogitem",
                    ),
                ),
                (
                    "stage",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="artifacts",
                        to="processing.processingstage",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="RemoteConsent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("notice_version", models.CharField(max_length=64)),
                (
                    "source_fingerprint",
                    models.CharField(max_length=64, validators=[SHA256_VALIDATOR]),
                ),
                ("payload_manifest", models.JSONField()),
                (
                    "payload_manifest_hash",
                    models.CharField(max_length=64, validators=[SHA256_VALIDATOR]),
                ),
                ("recipients", models.JSONField()),
                ("model_ids", models.JSONField()),
                ("prompt_versions", models.JSONField(blank=True, default=dict)),
                ("planned_request_count", models.PositiveIntegerField()),
                ("consented_at", models.DateTimeField(auto_now_add=True)),
                (
                    "catalog_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="remote_consents",
                        to="catalog.catalogitem",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="remote_consents",
                        to="processing.processingrun",
                    ),
                ),
            ],
            options={"ordering": ["-consented_at", "id"]},
        ),
        migrations.CreateModel(
            name="TimestampClaim",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("namespace", models.CharField(max_length=128)),
                ("tag", models.CharField(max_length=255)),
                ("raw_value", models.TextField()),
                (
                    "precision",
                    models.CharField(
                        choices=[
                            ("unknown", "Unknown"),
                            ("year", "Year"),
                            ("month", "Month"),
                            ("day", "Day"),
                            ("minute", "Minute"),
                            ("second", "Second"),
                            ("subsecond", "Subsecond"),
                        ],
                        default="unknown",
                        max_length=16,
                    ),
                ),
                (
                    "parsed_value",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        validators=[pandect.catalog.models.validate_timezone_aware],
                    ),
                ),
                ("timezone_evidence", models.TextField(blank=True)),
                (
                    "confidence",
                    models.FloatField(
                        blank=True,
                        null=True,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(1),
                        ],
                    ),
                ),
                ("conflicts", models.JSONField(blank=True, default=list)),
                ("provenance", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "catalog_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timestamp_claims",
                        to="catalog.catalogitem",
                    ),
                ),
                (
                    "stage",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timestamp_claims",
                        to="processing.processingstage",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.CreateModel(
            name="TimestampDecision",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "previous_occurred_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        validators=[pandect.catalog.models.validate_timezone_aware],
                    ),
                ),
                ("timezone_name", models.CharField(blank=True, max_length=64)),
                ("fold", models.PositiveSmallIntegerField(blank=True, null=True)),
                (
                    "resulting_occurred_at",
                    models.DateTimeField(
                        validators=[pandect.catalog.models.validate_timezone_aware]
                    ),
                ),
                ("operator_note", models.TextField(blank=True)),
                ("decided_at", models.DateTimeField(auto_now_add=True)),
                (
                    "catalog_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="timestamp_decisions",
                        to="catalog.catalogitem",
                    ),
                ),
                (
                    "claim",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="decisions",
                        to="processing.timestampclaim",
                    ),
                ),
            ],
            options={"ordering": ["decided_at", "id"]},
        ),
        migrations.AddIndex(
            model_name="processingrun",
            index=models.Index(
                fields=["catalog_item", "-created_at"],
                name="processing_run_item_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="processingrun",
            index=models.Index(
                fields=["status", "created_at"],
                name="processing_run_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="processingrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("finished_at__isnull", True),
                    ("status__in", ("succeeded", "partial", "failed", "superseded")),
                    _connector="OR",
                ),
                name="processing_run_finished_terminal",
            ),
        ),
        migrations.AddIndex(
            model_name="processingstage",
            index=models.Index(
                fields=["run", "status"],
                name="processing_stage_run_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="processingstage",
            index=models.Index(
                fields=["status", "lease_expires_at"],
                name="processing_stage_lease_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="processingstage",
            index=models.Index(
                fields=["kind", "status"],
                name="processing_stage_kind_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="processingstage",
            constraint=models.UniqueConstraint(
                fields=("run", "kind", "unit_key"),
                name="processing_unique_stage_unit",
            ),
        ),
        migrations.AddConstraint(
            model_name="processingstage",
            constraint=models.CheckConstraint(
                condition=models.Q(("max_attempts__gte", 1)),
                name="processing_stage_has_attempt",
            ),
        ),
        migrations.AddConstraint(
            model_name="processingstage",
            constraint=models.CheckConstraint(
                condition=models.Q(("attempts__lte", models.F("max_attempts"))),
                name="processing_stage_attempt_limit",
            ),
        ),
        migrations.AddConstraint(
            model_name="processingstage",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("finished_at__isnull", True),
                    (
                        "status__in",
                        ("succeeded", "failed", "skipped", "superseded"),
                    ),
                    _connector="OR",
                ),
                name="processing_stage_finished_terminal",
            ),
        ),
        migrations.AddIndex(
            model_name="processingresult",
            index=models.Index(
                fields=["stage", "result_kind"],
                name="processing_result_stage_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="processingresult",
            index=models.Index(
                fields=["result_kind", "created_at"],
                name="processing_result_kind_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="processingresult",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("confidence__isnull", True),
                    models.Q(("confidence__gte", 0), ("confidence__lte", 1)),
                    _connector="OR",
                ),
                name="processing_result_confidence_range",
            ),
        ),
        migrations.AddIndex(
            model_name="derivedartifact",
            index=models.Index(
                fields=["catalog_item", "role"],
                name="processing_artifact_item_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="derivedartifact",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_start_seconds__isnull", True),
                    ("source_start_seconds__gte", 0),
                    _connector="OR",
                ),
                name="processing_artifact_start_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="derivedartifact",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_end_seconds__isnull", True),
                    ("source_end_seconds__gte", 0),
                    _connector="OR",
                ),
                name="processing_artifact_end_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="derivedartifact",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("source_start_seconds__isnull", True),
                    ("source_end_seconds__isnull", True),
                    ("source_end_seconds__gte", models.F("source_start_seconds")),
                    _connector="OR",
                ),
                name="processing_artifact_ordered_range",
            ),
        ),
        migrations.AddIndex(
            model_name="remoteconsent",
            index=models.Index(
                fields=["run", "-consented_at"],
                name="processing_consent_run_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="timestampclaim",
            index=models.Index(
                fields=["catalog_item", "created_at"],
                name="processing_timestamp_item_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="timestampclaim",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("confidence__isnull", True),
                    models.Q(("confidence__gte", 0), ("confidence__lte", 1)),
                    _connector="OR",
                ),
                name="processing_timestamp_confidence_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="timestampdecision",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("fold__isnull", True),
                    ("fold__in", (0, 1)),
                    _connector="OR",
                ),
                name="processing_timestamp_fold_valid",
            ),
        ),
    ]

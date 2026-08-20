import uuid
from itertools import islice

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

import pandect.catalog.models


def preserve_existing_references(apps, schema_editor):
    SubjectReference = apps.get_model("identities", "SubjectReference")
    SubjectReferenceDecision = apps.get_model("identities", "SubjectReferenceDecision")
    references = SubjectReference.objects.select_related("subject").iterator(
        chunk_size=500
    )
    while batch := list(islice(references, 500)):
        SubjectReferenceDecision.objects.bulk_create(
            [
                SubjectReferenceDecision(
                    reference_id=reference.pk,
                    observation_id=reference.observation_id,
                    subject_id=reference.subject_id,
                    action="selected",
                    subject_label=reference.subject.label,
                    notes=reference.notes,
                    decided_at=reference.selected_at,
                )
                for reference in batch
            ],
            batch_size=500,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("identities", "0002_candidate_integrity"),
    ]

    operations = [
        migrations.CreateModel(
            name="SubjectReferenceDecision",
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
                    "reference_id",
                    models.UUIDField(
                        help_text=(
                            "Stable identifier of the reference row at decision time."
                        )
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("selected", "Selected as reference"),
                            ("updated", "Reference updated"),
                            ("removed", "Reference removed"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "subject_label",
                    models.CharField(
                        max_length=255,
                        validators=[pandect.catalog.models.validate_non_whitespace],
                    ),
                ),
                (
                    "previous_subject_label",
                    models.CharField(blank=True, max_length=255),
                ),
                ("notes", models.TextField(blank=True)),
                ("previous_notes", models.TextField(blank=True)),
                (
                    "decided_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        validators=[pandect.catalog.models.validate_timezone_aware],
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "observation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reference_decision_history",
                        to="identities.identityobservation",
                    ),
                ),
                (
                    "previous_subject",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reference_previous_decision_history",
                        to="identities.subject",
                    ),
                ),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reference_decision_history",
                        to="identities.subject",
                    ),
                ),
            ],
            options={
                "ordering": ["decided_at", "created_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="subjectreferencedecision",
            index=models.Index(
                fields=["observation", "-decided_at"],
                name="identity_ref_decision_obs_idx",
            ),
        ),
        migrations.RunPython(
            preserve_existing_references,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.CreateModel(
            name="IdentityGalleryRematchJob",
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
                    "modality",
                    models.CharField(
                        choices=[
                            ("face", "Face"),
                            ("voice", "Voice"),
                            ("writing", "Writing"),
                        ],
                        max_length=16,
                    ),
                ),
                ("upper_item_id", models.UUIDField(blank=True, null=True)),
                ("cursor_item_id", models.UUIDField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("runs_created", models.PositiveIntegerField(default=0)),
                ("failure_count", models.PositiveSmallIntegerField(default=0)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "completed_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        validators=[pandect.catalog.models.validate_timezone_aware],
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "decision",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rematch_job",
                        to="identities.subjectreferencedecision",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="identitygalleryrematchjob",
            index=models.Index(
                fields=["status", "lease_expires_at"],
                name="identity_gallery_job_lease_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="identitygalleryrematchjob",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(status="succeeded", completed_at__isnull=False)
                    | models.Q(
                        status__in=("pending", "running"),
                        completed_at__isnull=True,
                    )
                ),
                name="identity_gallery_job_completion",
            ),
        ),
    ]

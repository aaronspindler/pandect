import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("processing", "0002_processingstage_remote_aggregate"),
    ]

    operations = [
        migrations.CreateModel(
            name="ArtifactPublication",
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
                ("attempt", models.PositiveSmallIntegerField()),
                ("file_name", models.CharField(max_length=500)),
                ("temporary_name", models.CharField(max_length=500)),
                (
                    "sha256",
                    models.CharField(
                        max_length=64,
                        validators=[
                            django.core.validators.RegexValidator(
                                code="invalid_sha256",
                                message=(
                                    "Enter a lowercase hexadecimal SHA-256 digest."
                                ),
                                regex="\\A[0-9a-f]{64}\\Z",
                            )
                        ],
                    ),
                ),
                ("byte_size", models.PositiveBigIntegerField()),
                ("source_device", models.PositiveBigIntegerField()),
                ("source_inode", models.PositiveBigIntegerField()),
                ("published_at", models.DateTimeField(auto_now_add=True)),
                (
                    "stage",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="artifact_publications",
                        to="processing.processingstage",
                    ),
                ),
            ],
            options={"ordering": ["published_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="artifactpublication",
            constraint=models.UniqueConstraint(
                fields=("stage", "attempt", "file_name"),
                name="processing_unique_artifact_publication",
            ),
        ),
        migrations.AddConstraint(
            model_name="artifactpublication",
            constraint=models.CheckConstraint(
                condition=models.Q(("attempt__gte", 1)),
                name="processing_artifact_publication_attempt",
            ),
        ),
        migrations.AddIndex(
            model_name="artifactpublication",
            index=models.Index(
                fields=["stage", "attempt"],
                name="processing_artifact_pub_idx",
            ),
        ),
    ]

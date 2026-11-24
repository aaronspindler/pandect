from uuid import uuid4

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0017_evidence_chat")]

    operations = [
        migrations.CreateModel(
            name="PendingFileDeletion",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("storage_key", models.CharField(max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_attempted_at", models.DateTimeField(null=True)),
            ],
        ),
        migrations.CreateModel(
            name="DeletionReview",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("kind", models.CharField(max_length=32)),
                ("evidence_id", models.UUIDField()),
                ("fingerprint", models.CharField(max_length=64)),
                ("versions", models.JSONField()),
                ("counts", models.JSONField()),
                ("analysis_count", models.PositiveIntegerField()),
                ("entry_count", models.PositiveIntegerField()),
                ("pages", ArrayField(base_field=models.JSONField(), size=None)),
                ("expires_at", models.DateTimeField(db_index=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=models.CASCADE, to=settings.AUTH_USER_MODEL
                    ),
                ),
                ("case", models.ForeignKey(on_delete=models.CASCADE, to="cases.case")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("case", "actor", "kind", "evidence_id"),
                        name="unique_evidence_deletion_review",
                    ),
                ],
            },
        ),
    ]

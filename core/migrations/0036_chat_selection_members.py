import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0035_analysis_report_index")]
    operations = [
        migrations.CreateModel(
            name="ChatSelectionMember",
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
                ("kind", models.CharField(max_length=32)),
                ("evidence_id", models.UUIDField()),
                ("event_at", models.DateTimeField(null=True)),
                ("date_only", models.BooleanField(default=False)),
                ("forwarded", models.BooleanField(default=False)),
                ("source_version", models.BigIntegerField()),
                (
                    "turn",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="selection_members",
                        to="core.chatturn",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["kind", "evidence_id"], name="chat_selected_evidence"
                    ),
                    models.Index(
                        fields=["turn", "event_at"], name="chat_selected_clock"
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("turn", "kind", "evidence_id"),
                        name="chat_selection_member",
                    )
                ],
            },
        )
    ]

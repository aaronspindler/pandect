import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0038_cloudflare_search")]

    operations = [
        migrations.CreateModel(
            name="NarrativeRelationship",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("case", models.ForeignKey(to="cases.case", on_delete=models.CASCADE)),
                ("result", models.ForeignKey(to="core.analysisresult", on_delete=models.CASCADE)),
                ("item_index", models.PositiveIntegerField()),
                ("fingerprint", models.CharField(max_length=64)),
                ("evidence_id", models.UUIDField()),
                ("subject", models.CharField(max_length=160)),
                ("predicate", models.CharField(max_length=24)),
                ("object", models.CharField(max_length=160)),
                ("qualification", models.CharField(max_length=16)),
                ("locator", models.JSONField()),
                ("start", models.PositiveIntegerField()),
                ("quote", models.CharField(max_length=1024)),
                ("available_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=["case", "fingerprint"], name="case_narrative_edge")],
                "indexes": [models.Index(fields=["case", "evidence_id"], name="case_narrative_source")],
            },
        )
    ]

from uuid import uuid4

from django.db import migrations, models


def seed_revisions(apps, schema_editor):
    cases = apps.get_model("cases", "Case").objects.using(
        schema_editor.connection.alias
    )
    for case_id in cases.values_list("pk", flat=True).iterator():
        cases.filter(pk=case_id).update(external_ai_revision=uuid4())


class Migration(migrations.Migration):
    dependencies = [("cases", "0009_case_external_analysis_efforts")]

    operations = [
        migrations.RemoveConstraint(model_name="case", name="cases_valid_ai_backend"),
        migrations.RemoveField(model_name="case", name="ai_backend"),
        migrations.RemoveField(model_name="case", name="search_generation_policy"),
        migrations.RemoveField(model_name="case", name="search_embedding_approval"),
        migrations.RemoveField(model_name="case", name="search_rerank_approval"),
        migrations.AddField(
            model_name="case",
            name="external_ai_enabled",
            field=models.BooleanField(default=False, db_default=False, editable=False),
        ),
        migrations.AddField(
            model_name="case",
            name="external_ai_revision",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(seed_revisions, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="case",
            name="external_ai_revision",
            field=models.UUIDField(default=uuid4, editable=False),
        ),
    ]

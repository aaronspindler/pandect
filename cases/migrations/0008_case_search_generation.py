from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cases", "0007_case_external_analysis_models")]
    operations = [
        migrations.AddField(
            model_name="case",
            name=name,
            field=models.JSONField(
                blank=True, db_default={}, default=dict, editable=False
            ),
        )
        for name in ("search_generation_policy", "search_rerank_approval")
    ]

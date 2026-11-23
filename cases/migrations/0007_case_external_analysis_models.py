from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cases", "0006_case_activity_events")]

    operations = [
        migrations.AddField(
            model_name="case",
            name="external_analysis_models",
            field=models.JSONField(
                blank=True, db_default={}, default=dict, editable=False
            ),
        ),
    ]

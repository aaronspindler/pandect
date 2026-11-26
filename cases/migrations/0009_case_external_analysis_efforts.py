from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cases", "0008_case_search_generation")]
    operations = [
        migrations.AddField(
            model_name="case",
            name="external_analysis_efforts",
            field=models.JSONField(
                blank=True, db_default={}, default=dict, editable=False
            ),
        ),
    ]

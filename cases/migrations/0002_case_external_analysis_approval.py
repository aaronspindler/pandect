from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cases", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="case",
            name="external_analysis_approval",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
    ]

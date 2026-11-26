from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0030_analysis_batch_scope")]

    operations = [
        migrations.AddField(
            model_name="chatturn",
            name="activity",
            field=models.JSONField(db_default={}, default=dict, editable=False),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0010_backfill_finding_review")]
    operations = [
        migrations.AddField(
            model_name="analysisfinding",
            name="hidden_from_catalogue",
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]

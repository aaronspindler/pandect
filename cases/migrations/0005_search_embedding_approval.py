from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cases", "0004_case_activity")]

    operations = [
        migrations.AddField(
            model_name="case",
            name="search_embedding_approval",
            field=models.JSONField(
                default=dict, db_default={}, blank=True, editable=False
            ),
        ),
    ]

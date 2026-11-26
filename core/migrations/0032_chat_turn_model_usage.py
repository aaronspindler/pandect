from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0031_chat_turn_activity")]

    operations = [
        migrations.AddField(
            model_name="chatturn",
            name="model_usage",
            field=models.JSONField(db_default=[], default=list, editable=False),
        ),
    ]

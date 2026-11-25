from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0027_pending_file_deletion_case")]

    operations = [
        AddIndexConcurrently(
            model_name="chatturn",
            index=models.Index(
                fields=("chat", "case", "-created_at"), name="chat_turn_latest_created"
            ),
        ),
    ]

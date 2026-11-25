from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0028_chat_turn_latest_created")]
    operations = [
        AddIndexConcurrently(
            model_name="analysisrun",
            index=models.Index(
                fields=["case", "-created_at", "id"], name="analysis_case_history"
            ),
        ),
    ]

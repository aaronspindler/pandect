from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0021_chat_source_evidence_lookup")]

    operations = [
        AddIndexConcurrently(
            model_name="chatsource",
            index=models.Index(fields=("result_id",), name="chat_source_result"),
        ),
    ]

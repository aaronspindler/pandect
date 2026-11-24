from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0020_analysis_parameter_lookup")]

    operations = [
        AddIndexConcurrently(
            model_name="chatsource",
            index=models.Index(
                fields=("evidence_kind", "evidence_id"), name="chat_source_evidence"
            ),
        ),
    ]

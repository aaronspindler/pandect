from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0018_pending_file_deletion")]

    operations = [
        AddIndexConcurrently(
            model_name="analysisrun",
            index=GinIndex(
                fields=["inputs"],
                opclasses=["jsonb_path_ops"],
                name="analysis_inputs_gin",
            ),
        ),
    ]

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0019_deletion_lookup_indexes")]

    operations = [
        AddIndexConcurrently(
            model_name="analysisrun",
            index=GinIndex(
                fields=["parameters"],
                opclasses=["jsonb_path_ops"],
                name="analysis_parameters_gin",
            ),
        ),
    ]

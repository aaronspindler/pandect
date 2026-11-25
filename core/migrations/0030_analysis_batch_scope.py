from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0029_analysis_case_history")]
    operations = [
        AddIndexConcurrently(
            model_name="caseanalysisbatch",
            index=GinIndex(
                fields=["scope"],
                opclasses=["jsonb_path_ops"],
                name="analysis_batch_scope_gin",
            ),
        ),
    ]

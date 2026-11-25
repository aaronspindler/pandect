from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("phone", "0010_call_case_time_index")]

    operations = [
        AddIndexConcurrently(
            model_name="facetimeevidence",
            index=models.Index(
                fields=("case", "occurred_at", "id"),
                condition=models.Q(is_archived=False, occurred_at__isnull=False),
                name="phone_facetime_case_time",
            ),
        ),
    ]

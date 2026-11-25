from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("geo", "0007_person_location_case_time_index")]

    operations = [
        AddIndexConcurrently(
            model_name="locationevidence",
            index=models.Index(
                fields=("case", "observed_at", "id"),
                condition=models.Q(is_archived=False, observed_at__isnull=False),
                name="geo_location_case_time",
            ),
        ),
    ]

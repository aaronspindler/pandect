from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("geo", "0005_vehicle_import_timestamp_formats")]

    operations = [
        AddIndexConcurrently(
            model_name="vehiclelocationevidence",
            index=models.Index(
                fields=("case", "observed_at", "id"),
                condition=models.Q(is_archived=False, observed_at__isnull=False),
                name="geo_vehicle_loc_case_time",
            ),
        ),
    ]

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identities", "0003_reference_decisions_and_gallery_rematch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="identityobservation",
            name="catalog_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="identity_observations",
                to="catalog.catalogitem",
            ),
        ),
    ]

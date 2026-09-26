import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("processing", "0003_artifactpublication"),
    ]

    operations = [
        migrations.AlterField(
            model_name="processingresult",
            name="stage",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="results",
                to="processing.processingstage",
            ),
        ),
        migrations.AlterField(
            model_name="derivedartifact",
            name="stage",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="artifacts",
                to="processing.processingstage",
            ),
        ),
        migrations.AlterField(
            model_name="derivedartifact",
            name="catalog_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="derived_artifacts",
                to="catalog.catalogitem",
            ),
        ),
        migrations.AlterField(
            model_name="timestampclaim",
            name="stage",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="timestamp_claims",
                to="processing.processingstage",
            ),
        ),
        migrations.AlterField(
            model_name="timestampclaim",
            name="catalog_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="timestamp_claims",
                to="catalog.catalogitem",
            ),
        ),
        migrations.AlterField(
            model_name="timestampdecision",
            name="claim",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="decisions",
                to="processing.timestampclaim",
            ),
        ),
        migrations.AlterField(
            model_name="timestampdecision",
            name="catalog_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="timestamp_decisions",
                to="catalog.catalogitem",
            ),
        ),
        migrations.AlterField(
            model_name="remoteconsent",
            name="run",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="remote_consents",
                to="processing.processingrun",
            ),
        ),
        migrations.AlterField(
            model_name="remoteconsent",
            name="catalog_item",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="remote_consents",
                to="catalog.catalogitem",
            ),
        ),
    ]

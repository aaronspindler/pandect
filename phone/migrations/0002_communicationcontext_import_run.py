import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        ("phone", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="communicationcontext",
            name="import_run",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="imported_message_contexts",
                to="core.analysisrun",
            ),
        ),
        migrations.AddIndex(
            model_name="communicationcontext",
            index=models.Index(
                fields=["import_run", "source_row"], name="communication_import_row"
            ),
        ),
    ]

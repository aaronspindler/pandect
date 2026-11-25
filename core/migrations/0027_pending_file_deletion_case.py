from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0026_scoped_submissions")]

    operations = [
        migrations.AddField(
            model_name="pendingfiledeletion",
            name="case_id",
            field=models.UUIDField(editable=False, null=True),
        ),
    ]

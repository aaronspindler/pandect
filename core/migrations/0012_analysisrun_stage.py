from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0011_analysisfinding_hidden_from_catalogue")]
    operations = [
        migrations.AddField(
            model_name="analysisrun",
            name="stage",
            field=models.CharField(
                choices=[
                    ("sources", "Checking sources"),
                    ("analysis", "Running analysis"),
                    ("publication", "Saving results"),
                ],
                db_default="analysis",
                default="analysis",
                max_length=16,
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0041_acknowledge_processing_failure")]

    operations = [
        migrations.AddField(
            model_name="hostedsearchindex",
            name="keyword_match_mode",
            field=models.CharField(
                max_length=3, default="and", db_default="or", editable=False
            ),
        ),
    ]

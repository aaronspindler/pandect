from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0008_case_search_embeddings")]
    operations = [
        migrations.AddField(
            model_name="analysisfinding",
            name="review_signal",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="analysisfinding",
            name="repeat_key",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]

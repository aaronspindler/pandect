from uuid import uuid4

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0042_hosted_search_keyword_mode")]

    operations = [
        migrations.CreateModel(
            name="CloudflareSearchRateLimit",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("account", models.CharField(max_length=32, unique=True)),
                ("scheduled_at", models.DateTimeField()),
                ("resume_at", models.DateTimeField(null=True)),
            ],
        ),
    ]

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0010_external_ai"),
        ("core", "0040_fence_retired_chat"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisrun",
            name="failure_acknowledged_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="analysisrun",
            name="failure_acknowledged_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="analysisrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("failure_acknowledged_at__isnull", True),
                        ("failure_acknowledged_by__isnull", True),
                    ),
                    models.Q(
                        ("failure_acknowledged_at__isnull", False),
                        ("failure_acknowledged_by__isnull", False),
                        ("status", "failed"),
                    ),
                    _connector="OR",
                ),
                name="analysis_failure_acknowledgment",
            ),
        ),
    ]

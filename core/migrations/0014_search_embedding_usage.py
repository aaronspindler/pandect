import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0012_analysisrun_stage")]

    operations = [
        migrations.AddField(
            model_name="gatewayrequest",
            name="operation",
            field=models.CharField(
                choices=[
                    ("analysis", "Analysis"),
                    ("search_index", "Meaning search indexing"),
                    ("search_query", "Meaning search query"),
                ],
                default="analysis",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="gatewayrequest",
            name="run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="gateway_requests",
                to="core.analysisrun",
            ),
        ),
        migrations.AddConstraint(
            model_name="gatewayrequest",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(operation="analysis", run__isnull=False)
                    | models.Q(
                        operation__in=["search_index", "search_query"], run__isnull=True
                    )
                ),
                name="gateway_usage_operation_run",
            ),
        ),
    ]

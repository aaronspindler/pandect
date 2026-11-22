from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cases", "0002_case_external_analysis_approval")]

    operations = [
        migrations.AddField(
            model_name="case",
            name="ai_backend",
            field=models.CharField(
                choices=[("local", "Local AI"), ("gateway", "External AI")],
                db_default="local",
                default="local",
                editable=False,
                max_length=7,
            ),
        ),
        migrations.AddConstraint(
            model_name="case",
            constraint=models.CheckConstraint(
                condition=models.Q(ai_backend__in=("local", "gateway")),
                name="cases_valid_ai_backend",
            ),
        ),
    ]

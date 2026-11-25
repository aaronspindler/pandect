from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("geo", "0004_scoped_submissions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vehiclelocationimport",
            name="parser_version",
            field=models.CharField(default="asset-location.v3", max_length=50),
        ),
    ]

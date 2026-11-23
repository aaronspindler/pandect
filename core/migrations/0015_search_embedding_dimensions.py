from django.db import migrations, models
from django.db.models.lookups import In
from pgvector.django import VectorField


class Migration(migrations.Migration):
    dependencies = [("core", "0014_search_embedding_usage")]

    operations = [
        migrations.AlterField(
            model_name="searchpassage", name="embedding", field=VectorField()
        ),
        migrations.AddConstraint(
            model_name="searchpassage",
            constraint=models.CheckConstraint(
                condition=In(
                    models.Func(
                        "embedding",
                        function="vector_dims",
                        output_field=models.IntegerField(),
                    ),
                    (384, 1536),
                ),
                name="case_search_embedding_dimensions",
            ),
        ),
        migrations.AddField(
            model_name="searchpassage",
            name="profile",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.RunSQL(
            """
            UPDATE core_searchpassage p SET profile = d.profile
            FROM core_searchdocument d WHERE d.id = p.document_id
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RemoveConstraint(
            model_name="searchpassage", name="case_search_offset"
        ),
        migrations.AddConstraint(
            model_name="searchpassage",
            constraint=models.UniqueConstraint(
                fields=["document", "profile", "start"],
                name="case_search_profile_offset",
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0014_search_embedding_usage")]

    operations = [
        # The obsolete embedding cache is dropped at the Cloudflare cutover.
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

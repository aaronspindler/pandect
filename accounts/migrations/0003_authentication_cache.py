"""Create the upstream Django database-cache schema for shared auth limits."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_user_timezone")]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE TABLE pandect_authentication_cache (
                    cache_key varchar(255) NOT NULL PRIMARY KEY,
                    value text NOT NULL,
                    expires timestamp with time zone NOT NULL
                );
                CREATE INDEX pandect_authentication_cache_expires
                    ON pandect_authentication_cache (expires);
            """,
            reverse_sql="DROP TABLE pandect_authentication_cache;",
        ),
    ]

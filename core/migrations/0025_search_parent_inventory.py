from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0024_chat_completed_topics")]

    operations = [
        migrations.RunSQL(
            """CREATE FUNCTION pandect_activity_search_parent() RETURNS trigger
               LANGUAGE plpgsql AS $$
               BEGIN
                   UPDATE cases_caseactivity SET work = work + 1
                   WHERE case_id IN (SELECT DISTINCT case_id FROM changed);
                   RETURN NULL;
               END $$;
               CREATE TRIGGER activity_search_parent_insert
                   AFTER INSERT ON core_searchparent
                   REFERENCING NEW TABLE AS changed
                   FOR EACH STATEMENT EXECUTE FUNCTION pandect_activity_search_parent();
               CREATE TRIGGER activity_search_parent_delete
                   AFTER DELETE ON core_searchparent
                   REFERENCING OLD TABLE AS changed
                   FOR EACH STATEMENT
                   EXECUTE FUNCTION pandect_activity_search_parent();""",
            """DROP TRIGGER activity_search_parent_delete ON core_searchparent;
               DROP TRIGGER activity_search_parent_insert ON core_searchparent;
               DROP FUNCTION pandect_activity_search_parent();""",
        )
    ]

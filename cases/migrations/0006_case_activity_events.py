"""Notify after a case revision commits, including Important marks and notes."""

from importlib import import_module

from django.db import migrations, models

change_triggers = import_module("cases.migrations.0004_case_activity").change_triggers


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0005_search_embedding_approval"),
        ("core", "0013_important_items"),
    ]

    operations = [
        migrations.AddField(
            model_name="caseactivity",
            name="important",
            field=models.BigIntegerField(db_default=0),
        ),
        *change_triggers("important", ("core_importantitem",), ("important",)),
        *change_triggers(
            "important_notes",
            ("core_importantnote",),
            ("important",),
            "changed JOIN core_importantitem i ON i.id = changed.item_id",
        ),
        migrations.RunSQL(
            """CREATE FUNCTION pandect_notify_case_activity() RETURNS trigger
               LANGUAGE plpgsql AS $$
               BEGIN
                   PERFORM pg_notify('pandect_case_activity', case_id::text)
                   FROM changed;
                   RETURN NULL;
               END $$;
               CREATE TRIGGER notify_case_activity AFTER UPDATE ON cases_caseactivity
                   REFERENCING NEW TABLE AS changed FOR EACH STATEMENT
                   EXECUTE FUNCTION pandect_notify_case_activity();""",
            """DROP TRIGGER notify_case_activity ON cases_caseactivity;
               DROP FUNCTION pandect_notify_case_activity();""",
        ),
    ]

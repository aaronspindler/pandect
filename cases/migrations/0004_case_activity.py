"""Keep cheap, transactional change markers through bulk writes and cascades."""

from uuid import uuid4

from django.db import migrations, models

FIELDS = ("work", "evidence", "images", "findings", "people", "vehicles")
EVIDENCE = (
    "audio_audioevidence",
    "document_documentevidence",
    "geo_locationevidence",
    "geo_personlocationevidence",
    "geo_vehiclelocationevidence",
    "phone_callevidenceattachment",
    "phone_facetimeevidence",
    "phone_phonecallevidence",
    "phone_textmessageevidence",
    "text_textevidence",
)
WORK = (
    "core_analysisrun",
    "core_caseanalysisbatch",
    "core_casesearchindex",
    "geo_vehiclelocationimport",
    "geo_vehicletripgeneration",
)


def change_triggers(name, tables, fields, source="changed", *, on_edit=()):
    # Each transition table includes the complete SQL statement. An import of
    # thousands of rows takes just one revision update per affected case.
    function = f"pandect_activity_{name}"
    assignments = [f"{field} = {field} + 1" for field in fields]
    # A newly retained source has no existing citations. Editing or removing
    # it also changes the source labels/availability of existing findings.
    assignments.extend(
        f"{field} = {field} + (TG_OP <> 'INSERT')::int" for field in on_edit
    )
    revisions = ", ".join(assignments)
    yield migrations.RunSQL(
        f"""CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE cases_caseactivity SET {revisions}
                WHERE case_id IN (SELECT DISTINCT case_id FROM {source});
                RETURN NULL;
            END $$""",
        f"DROP FUNCTION {function}()",
    )
    for table in tables:
        for action, source_rows in (
            ("INSERT", "NEW"),
            ("UPDATE", "NEW"),
            ("DELETE", "OLD"),
        ):
            trigger = f"activity_{name}_{action.lower()}"
            yield migrations.RunSQL(
                f"""CREATE TRIGGER {trigger} AFTER {action} ON {table}
                    REFERENCING {source_rows} TABLE AS changed
                    FOR EACH STATEMENT EXECUTE FUNCTION {function}()""",
                f"DROP TRIGGER {trigger} ON {table}",
            )


def active_index(table, field, condition):
    name = f"activity_{table}_active"
    return migrations.RunSQL(
        f"CREATE INDEX {name} ON {table} ({field}) WHERE {condition}",
        f"DROP INDEX {name}",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("cases", "0003_case_ai_backend"),
        ("audio", "0001_initial"),
        ("core", "0008_case_search_embeddings"),
        ("document", "0001_initial"),
        ("geo", "0002_saved_vehicle_trips"),
        ("image", "0001_initial"),
        ("people", "0001_initial"),
        ("phone", "0001_initial"),
        ("socialmedia", "0003_instagram_monitor"),
        ("text", "0001_initial"),
        ("vehicle", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="CaseActivity",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True, default=uuid4, editable=False, serialize=False
                    ),
                ),
                (
                    "case",
                    models.OneToOneField(
                        on_delete=models.CASCADE,
                        related_name="activity",
                        to="cases.case",
                    ),
                ),
                *((name, models.BigIntegerField(db_default=0)) for name in FIELDS),
            ],
        ),
        migrations.RunSQL(
            """CREATE FUNCTION pandect_case_activity() RETURNS trigger
               LANGUAGE plpgsql AS $$
               BEGIN
                   IF TG_OP = 'INSERT' THEN
                       INSERT INTO cases_caseactivity (id, case_id)
                       SELECT gen_random_uuid(), id FROM changed;
                   ELSE
                       DELETE FROM cases_caseactivity
                       WHERE case_id IN (SELECT id FROM changed);
                   END IF;
                   RETURN NULL;
               END $$;
               CREATE TRIGGER init_case_activity AFTER INSERT ON cases_case
                   REFERENCING NEW TABLE AS changed
                   FOR EACH STATEMENT EXECUTE FUNCTION pandect_case_activity();
               CREATE TRIGGER delete_case_activity AFTER DELETE ON cases_case
                   REFERENCING OLD TABLE AS changed
                   FOR EACH STATEMENT EXECUTE FUNCTION pandect_case_activity();
               INSERT INTO cases_caseactivity (id, case_id)
                   SELECT gen_random_uuid(), id FROM cases_case;""",
            """DROP TRIGGER init_case_activity ON cases_case;
               DROP TRIGGER delete_case_activity ON cases_case;
               DROP FUNCTION pandect_case_activity()""",
        ),
        *change_triggers("work", (*WORK, "socialmedia_instagrammonitor"), ("work",)),
        *change_triggers("evidence", EVIDENCE, ("evidence",), on_edit=("findings",)),
        *change_triggers(
            "images",
            ("image_imageevidence",),
            ("evidence", "images"),
            on_edit=("findings",),
        ),
        *change_triggers("passages", ("core_extractedpassage",), ("evidence",)),
        *change_triggers(
            "image_passages",
            ("core_extractedpassage",),
            ("images",),
            "changed WHERE kind = 'image'",
        ),
        *change_triggers(
            "findings",
            ("core_analysisresult", "core_analysisfinding", "core_analysisreview"),
            ("findings",),
        ),
        *change_triggers(
            "people", ("people_person",), ("people",), on_edit=("evidence", "findings")
        ),
        *change_triggers(
            "vehicles",
            ("vehicle_vehicle",),
            ("vehicles",),
            on_edit=("evidence", "findings"),
        ),
        *change_triggers(
            "accounts",
            ("socialmedia_socialaccount",),
            ("people",),
            "changed JOIN people_person ON person_id = people_person.id",
        ),
        *change_triggers(
            "monitors",
            ("socialmedia_instagramcheck",),
            ("work",),
            "changed JOIN socialmedia_instagrammonitor m ON monitor_id = m.id",
        ),
        active_index(
            "core_analysisrun", "case_id, status", "status IN ('queued', 'running')"
        ),
        active_index("core_caseanalysisbatch", "case_id", "status = 'queued'"),
        active_index(
            "geo_vehiclelocationimport",
            "case_id",
            "status IN ('pending', 'queued', 'running')",
        ),
        active_index(
            "geo_vehicletripgeneration",
            "case_id",
            "status IN ('queued', 'running') OR "
            "(status = 'complete' AND geocode_status IN ('queued', 'running'))",
        ),
        active_index("socialmedia_instagramcheck", "monitor_id", "status = 'running'"),
    ]

"""Track grouping inputs through ordinary and bulk writes."""

from uuid import uuid4

from django.db import migrations, models

PAIR_FIELDS = ("case_id", "sender_id", "recipient_id", "is_archived")
ORIGINAL_FIELDS = (*PAIR_FIELDS, "occurred_at", "sha256", "body")


def changed_rows(name, action):
    fields = PAIR_FIELDS if name == "pair_messages" else ("message_id",)
    columns = ", ".join(fields)
    match action:
        case "insert":
            return f"SELECT {columns} FROM added"
        case "delete":
            return f"SELECT {columns} FROM removed"
        case "update" if name != "pair_messages":
            return (
                f"SELECT {columns} FROM removed UNION ALL SELECT {columns} FROM added"
            )
        case "update":
            old_pair = ", ".join(f"prior.{field}" for field in fields)
            new_pair = ", ".join(f"updated.{field}" for field in fields)
            left = ", ".join(f"prior.{field}" for field in ORIGINAL_FIELDS)
            right = ", ".join(f"updated.{field}" for field in ORIGINAL_FIELDS)
            return f"""
                SELECT versions.* FROM removed prior
                FULL JOIN added updated USING (id)
                CROSS JOIN LATERAL
                    (VALUES ({old_pair}), ({new_pair})) versions({columns})
                WHERE versions.case_id IS NOT NULL
                  AND ROW({left}) IS DISTINCT FROM ROW({right})
            """


def revision_triggers():
    tables = {
        "pair_messages": "phone_textmessageevidence",
        "pair_context": "phone_communicationcontext",
    }
    for name, table in tables.items():
        for action, transitions in (
            ("insert", "NEW TABLE AS added"),
            ("update", "OLD TABLE AS removed NEW TABLE AS added"),
            ("delete", "OLD TABLE AS removed"),
        ):
            function = f"pandect_{name}_{action}"
            rows = changed_rows(name, action)
            messages = (
                f"({rows}) changed"
                if name == "pair_messages"
                else (
                    f"({rows}) changed JOIN phone_textmessageevidence m "
                    "ON m.id = changed.message_id"
                )
            )
            source = "changed" if name == "pair_messages" else "m"
            yield migrations.RunSQL(
                f"""
                CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    INSERT INTO phone_conversationpairrevision
                        (id, case_id, first_person_id, second_person_id, revision)
                    SELECT gen_random_uuid(), pairs.*, gen_random_uuid()
                    FROM (
                        SELECT DISTINCT {source}.case_id,
                            least({source}.sender_id, {source}.recipient_id),
                            greatest({source}.sender_id, {source}.recipient_id)
                        FROM {messages}
                        WHERE NOT {source}.is_archived
                    ) pairs
                    ORDER BY 2, 3, 4
                    ON CONFLICT (case_id, first_person_id, second_person_id)
                    DO UPDATE SET revision = excluded.revision;
                    RETURN NULL;
                END $$;
                CREATE TRIGGER {function} AFTER {action.upper()} ON {table}
                    REFERENCING {transitions}
                    FOR EACH STATEMENT EXECUTE FUNCTION {function}();
                """,
                f"DROP TRIGGER {function} ON {table}; DROP FUNCTION {function}();",
            )


class Migration(migrations.Migration):
    dependencies = [
        ("phone", "0005_conversation_grouping"),
        ("cases", "0007_case_external_analysis_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConversationPairRevision",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("first_person_id", models.UUIDField()),
                ("second_person_id", models.UUIDField()),
                ("revision", models.UUIDField(default=uuid4)),
                (
                    "case",
                    models.ForeignKey(on_delete=models.CASCADE, to="cases.case"),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("case", "first_person_id", "second_person_id"),
                        name="phone_pair_revision_unique",
                    )
                ]
            },
        ),
        *revision_triggers(),
        # Django may collect pair markers before deleting the messages that
        # advance them. Remove newly recreated markers at the case boundary.
        migrations.RunSQL(
            """
            CREATE FUNCTION pandect_delete_pair_revisions() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                DELETE FROM phone_conversationpairrevision WHERE case_id = OLD.id;
                RETURN NULL;
            END $$;
            CREATE TRIGGER pandect_delete_pair_revisions AFTER DELETE ON cases_case
                FOR EACH ROW EXECUTE FUNCTION pandect_delete_pair_revisions();
            """,
            """
            DROP TRIGGER pandect_delete_pair_revisions ON cases_case;
            DROP FUNCTION pandect_delete_pair_revisions();
            """,
        ),
    ]

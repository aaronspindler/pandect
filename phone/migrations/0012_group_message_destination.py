"""Retain a group destination without changing direct message originals."""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models
from django.db.models.functions import Greatest, Least

changed_rows = import_module(
    "phone.migrations.0006_conversation_pair_revision"
).changed_rows


def pair_revision_functions(*, direct_only):
    for name in ("pair_messages", "pair_context"):
        for action in ("insert", "update", "delete"):
            function = f"pandect_{name}_{action}"
            rows = changed_rows(name, action)
            source = "changed" if name == "pair_messages" else "m"
            messages = f"({rows}) changed"
            if name == "pair_context":
                messages += (
                    " JOIN phone_textmessageevidence m ON m.id = changed.message_id"
                )
            conditions = f"NOT {source}.is_archived"
            if direct_only:
                conditions += f" AND {source}.recipient_id IS NOT NULL"
            yield f"""
                CREATE OR REPLACE FUNCTION {function}() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    INSERT INTO phone_conversationpairrevision
                        (id, case_id, first_person_id, second_person_id, revision)
                    SELECT gen_random_uuid(), pairs.*, gen_random_uuid()
                    FROM (
                        SELECT DISTINCT {source}.case_id,
                            least({source}.sender_id, {source}.recipient_id),
                            greatest({source}.sender_id, {source}.recipient_id)
                        FROM {messages}
                        WHERE {conditions}
                    ) pairs
                    ORDER BY 2, 3, 4
                    ON CONFLICT (case_id, first_person_id, second_person_id)
                    DO UPDATE SET revision = excluded.revision;
                    RETURN NULL;
                END $$;
            """


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0003_scoped_submissions"),
        ("phone", "0011_facetime_case_time_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="textmessageevidence",
            name="group_source",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="group_messages",
                to="document.documentevidence",
            ),
        ),
        migrations.AlterField(
            model_name="textmessageevidence",
            name="recipient",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="%(class)s_received",
                to="people.person",
            ),
        ),
        migrations.AddConstraint(
            model_name="textmessageevidence",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(recipient__isnull=False, group_source__isnull=True)
                    | models.Q(recipient__isnull=True, group_source__isnull=False)
                ),
                name="message_one_destination",
            ),
        ),
        migrations.RemoveIndex(
            model_name="textmessageevidence", name="phone_active_pair_time"
        ),
        migrations.AddIndex(
            model_name="textmessageevidence",
            index=models.Index(
                "case",
                Least("sender_id", "recipient_id"),
                Greatest("sender_id", "recipient_id"),
                "occurred_at",
                "id",
                condition=models.Q(is_archived=False, recipient__isnull=False),
                name="phone_active_pair_time",
            ),
        ),
        migrations.RunSQL(
            list(pair_revision_functions(direct_only=True)),
            list(pair_revision_functions(direct_only=False)),
        ),
    ]

from itertools import batched
from uuid import uuid4

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import migrations, models


def index_existing(apps, schema_editor):
    from core.analysis.report_index import measurements_v1, timing_values_v1

    db = schema_editor.connection.alias
    Result = apps.get_model("core", "AnalysisResult")
    Index = apps.get_model("core", "AnalysisReportIndex")
    query = Result.objects.using(db).filter(report_index__isnull=True).order_by("pk")
    values = query.values_list("pk", "run__operation", "data__metrics")
    for rows in batched(values.iterator(chunk_size=200), 200, strict=False):
        Index.objects.using(db).bulk_create(
            [
                Index(
                    result_id=pk,
                    text=measurements_v1(operation, metrics),
                    timing_values=timing_values_v1(operation, metrics),
                )
                for pk, operation, metrics in rows
            ],
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0034_approved_chat_extraction")]
    operations = [
        migrations.CreateModel(
            name="AnalysisReportIndex",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("text", models.TextField()),
                ("verified_messages", models.JSONField(default=dict)),
                ("timing_values", models.JSONField(null=True)),
                (
                    "result",
                    models.OneToOneField(
                        on_delete=models.CASCADE,
                        related_name="report_index",
                        to="core.analysisresult",
                    ),
                ),
            ],
            options={
                "indexes": [
                    GinIndex(
                        SearchVector("text", config="english"),
                        name="analysis_report_words",
                    )
                ],
            },
        ),
        migrations.RunSQL(
            """
            CREATE FUNCTION pandect_invalidate_report_members() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE core_analysisreportindex i
                   SET verified_messages=jsonb_build_object('version',
                       COALESCE((i.verified_messages->>'version')::bigint, 0)+1)
                  FROM core_analysisresult r
                 WHERE r.run_id=NEW.id AND r.id=i.result_id;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER invalidate_report_members AFTER UPDATE OF inputs
            ON core_analysisrun FOR EACH ROW
            WHEN (OLD.inputs IS DISTINCT FROM NEW.inputs)
            EXECUTE FUNCTION pandect_invalidate_report_members();

            -- The ordinary activity triggers bump the new case on UPDATE.
            -- Also invalidate the old case if a previously selected message or
            -- either of its parties moves out of that case.
            CREATE FUNCTION pandect_invalidate_prior_message_case() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE cases_caseactivity SET evidence=evidence+1
                 WHERE case_id=OLD.case_id;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER invalidate_prior_message_case AFTER UPDATE OF case_id
            ON phone_textmessageevidence FOR EACH ROW
            WHEN (OLD.case_id IS DISTINCT FROM NEW.case_id)
            EXECUTE FUNCTION pandect_invalidate_prior_message_case();
            CREATE TRIGGER invalidate_prior_message_case AFTER UPDATE OF case_id
            ON people_person FOR EACH ROW
            WHEN (OLD.case_id IS DISTINCT FROM NEW.case_id)
            EXECUTE FUNCTION pandect_invalidate_prior_message_case();
            """,
            """
            DROP TRIGGER invalidate_prior_message_case ON phone_textmessageevidence;
            DROP TRIGGER invalidate_prior_message_case ON people_person;
            DROP FUNCTION pandect_invalidate_prior_message_case();
            DROP TRIGGER invalidate_report_members ON core_analysisrun;
            DROP FUNCTION pandect_invalidate_report_members();
            """,
        ),
        migrations.RunPython(index_existing, migrations.RunPython.noop),
    ]

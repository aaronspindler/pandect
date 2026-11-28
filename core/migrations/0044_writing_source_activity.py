from importlib import import_module

from django.db import migrations

change_triggers = import_module("cases.migrations.0004_case_activity").change_triggers


class Migration(migrations.Migration):
    dependencies = [("core", "0043_cloudflare_search_rate_limit")]

    operations = [
        *change_triggers(
            "message_context", ("phone_communicationcontext",), ("evidence",)
        ),
        migrations.RunSQL(
            """
            CREATE TRIGGER invalidate_prior_message_context_case
                AFTER UPDATE OF case_id ON phone_communicationcontext FOR EACH ROW
                WHEN (OLD.case_id IS DISTINCT FROM NEW.case_id)
                EXECUTE FUNCTION pandect_invalidate_prior_message_case();
            CREATE TRIGGER invalidate_prior_group_document_case
                AFTER UPDATE OF case_id ON document_documentevidence FOR EACH ROW
                WHEN (OLD.case_id IS DISTINCT FROM NEW.case_id)
                EXECUTE FUNCTION pandect_invalidate_prior_message_case();
            """,
            """
            DROP TRIGGER invalidate_prior_message_context_case
                ON phone_communicationcontext;
            DROP TRIGGER invalidate_prior_group_document_case
                ON document_documentevidence;
            """,
        ),
    ]

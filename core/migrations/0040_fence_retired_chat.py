from django.db import migrations
from django.utils import timezone


def fence_retired_turns(apps, schema_editor):
    turns = apps.get_model("core", "ChatTurn")
    turns.objects.using(schema_editor.connection.alias).filter(
        status__in=("queued", "running"),
    ).exclude(plan__has_key="agent_version").update(
        status="failed", completed_at=timezone.now(), error_code="chat_agent_cutover"
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0039_narrative_relationships")]
    operations = [migrations.RunPython(fence_retired_turns, migrations.RunPython.noop)]

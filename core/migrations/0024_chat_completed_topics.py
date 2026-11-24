from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.contrib.postgres.search import SearchVector
from django.db import migrations, models
from django.db.models.fields.json import KT
from django.db.models.functions import Left


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0023_search_parents")]
    operations = [
        AddIndexConcurrently(
            model_name="chatturn",
            index=GinIndex(
                SearchVector(
                    Left("question", 300),
                    Left(KT("answer__clarification"), 400),
                    Left(KT("answer__lead"), 200),
                    Left(KT("answer__claims__0__text"), 400),
                    Left(KT("answer__claims__1__text"), 400),
                    config="simple",
                ),
                condition=models.Q(status="complete"),
                name="chat_completed_topics",
            ),
        ),
    ]

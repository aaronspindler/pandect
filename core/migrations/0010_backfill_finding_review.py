"""Populate review projections from retained results, resuming after each result."""

from itertools import batched

from django.db import migrations, transaction
from django.db.models import Exists, F, OuterRef

from core.analysis.review_signals import review_fields_v1


def backfill(apps, schema_editor):
    Result = apps.get_model("core", "AnalysisResult")
    Finding = apps.get_model("core", "AnalysisFinding")
    alias = schema_editor.connection.alias
    missing = Finding.objects.using(alias).filter(repeat_key="")
    results = (
        Result.objects.using(alias)
        .filter(
            Exists(
                missing.filter(result_id=OuterRef("pk"), case_id=OuterRef("case_id"))
            ),
            case_id=F("run__case_id"),
        )
        .select_related("run")
        .only("case_id", "data", "run__operation", "run__inputs")
    )
    for result in results.iterator(chunk_size=8):
        inputs = {
            source["id"]: source
            for source in result.run.inputs
            if isinstance(source, dict) and "id" in source
        }
        items = result.data.get("items", []) if isinstance(result.data, dict) else []
        with transaction.atomic(using=alias):
            rows = (
                missing.filter(result_id=result.pk, case_id=result.case_id)
                .only(
                    "pk",
                    "item_index",
                    "match_kind",
                    "match_key",
                )
                .order_by("item_index")
            )
            for chunk in batched(rows.iterator(chunk_size=500), 500, strict=False):
                changed = []
                for row in chunk:
                    if (
                        not isinstance(items, list)
                        or row.item_index >= len(items)
                        or not isinstance(items[row.item_index], dict)
                    ):
                        continue
                    fields = review_fields_v1(
                        result.run.operation,
                        items[row.item_index],
                        inputs,
                        row.match_kind,
                        row.match_key,
                    )
                    row.review_signal = fields["review_signal"]
                    row.repeat_key = fields["repeat_key"]
                    changed.append(row)
                Finding.objects.using(alias).bulk_update(
                    changed, ["review_signal", "repeat_key"], batch_size=500
                )


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0009_finding_review_fields")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]

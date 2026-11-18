"""Classify retained timing-only observations without replacing saved results."""

import math
from itertools import batched

from django.db import migrations, transaction
from django.db.models import F


def is_timing_only(item):
    """Frozen classification; unknown shapes remain visible."""
    if not isinstance(item, dict) or item.get("label") != "Candidate exchange sequence":
        return False
    values = item.get("values")
    if not isinstance(values, dict):
        return False
    elapsed = values.get("elapsed_seconds")
    return (
        "status" not in values
        and isinstance(values.get("before"), dict)
        and isinstance(values.get("after"), dict)
        and type(elapsed) in (int, float)
        and (type(elapsed) is int or math.isfinite(elapsed))
        and elapsed >= 0
        and values.get("shared_words") == []
        and not values["before"].get("reply_to_id")
        and not values["after"].get("reply_to_id")
    )


def update_classification(apps, schema_editor, *, include_timing):
    Result = apps.get_model("core", "AnalysisResult")
    Finding = apps.get_model("core", "AnalysisFinding")
    alias = schema_editor.connection.alias
    results = (
        Result.objects.using(alias)
        .filter(run__operation="conversation_link", case_id=F("run__case_id"))
        .only("pk", "case_id", "data")
        .order_by("pk")
    )
    for result in results.iterator(chunk_size=20):
        data = result.data if isinstance(result.data, dict) else {}
        items = data.get("items", [])
        if not isinstance(items, list):
            items = []
        with transaction.atomic(using=alias):
            findings = (
                Finding.objects.using(alias)
                .filter(result_id=result.pk, case_id=result.case_id)
                .only("pk", "item_index", "weak")
                .order_by("pk")
            )
            for chunk in batched(findings.iterator(chunk_size=500), 500, strict=False):
                changed = []
                for finding in chunk:
                    item = (
                        items[finding.item_index]
                        if finding.item_index < len(items)
                        else {}
                    )
                    values = item.get("values", {}) if isinstance(item, dict) else {}
                    plate = (
                        isinstance(values, dict)
                        and values.get("Weak plate region") is True
                    )
                    weak = plate or (include_timing and is_timing_only(item))
                    if finding.weak != weak:
                        finding.weak = weak
                        changed.append(finding)
                Finding.objects.using(alias).bulk_update(
                    changed, ["weak"], batch_size=500
                )


def forwards(apps, schema_editor):
    update_classification(apps, schema_editor, include_timing=True)


def backwards(apps, schema_editor):
    update_classification(apps, schema_editor, include_timing=False)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0005_saved_selection")]
    operations = [migrations.RunPython(forwards, backwards)]

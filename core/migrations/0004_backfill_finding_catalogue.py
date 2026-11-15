"""Frozen v1 projection backfill; reads saved JSON only and resumes per run."""

import hashlib
import json
import re
from itertools import batched
from urllib.parse import urlsplit, urlunsplit

from django.db import migrations, transaction


def equivalence_key(run):
    """Order and exact retained versions are meaningful analysis inputs."""
    selection_keys = (
        "kind",
        "evidence_id",
        "attachment_id",
        "result_id",
        "geocode_candidate",
        "review_id",
        "sha256",
    )
    value = {
        "version": 1,
        "operation": run.operation,
        "method_version": run.method_version,
        "external_processing": run.external_processing,
        "parameters": {
            key: value
            for key, value in run.parameters.items()
            if key not in {"automatic_batch", "automatic_followup"}
        },
        "inputs": [
            {key: source[key] for key in selection_keys if key in source}
            for source in run.inputs
        ],
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def scalar_text(value):
    if isinstance(value, dict):
        return "\n".join(f"{key}: {scalar_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(scalar_text(item) for item in value)
    return "" if value is None else str(value)


def identifier_key(operation, item):
    """Comparison candidates only; no entity resolution or country inference."""
    values = item.get("values", {})
    label = item.get("label", "")
    raw = values.get("raw", item.get("text", ""))
    if not isinstance(raw, str):
        return "", ""
    if operation == "plate_ocr":
        if values.get("Weak plate region") or not item.get("text"):
            return "", ""
        key = "".join(c.upper() for c in raw if c.isascii() and c.isalnum())
        return ("plate", key) if 3 <= len(key) <= 12 else ("", "")
    if operation != "entities":
        return "", ""
    if label == "Email" and "@" in raw:
        local, domain = raw.rsplit("@", 1)
        return "email", f"{local}@{domain.lower()}"
    if label == "Domain candidate":
        return "domain", raw.lower()
    if label == "Phone candidate":
        # Preserve whether the source explicitly used an international prefix.
        digits = re.sub(r"[^0-9]", "", raw)
        return "phone", ("+" if raw.startswith("+") else "") + digits
    if label == "Identifier candidate":
        return "identifier", raw
    if label == "URL":
        try:
            parsed = urlsplit(raw)
            if not parsed.hostname or parsed.username or parsed.password:
                return "", ""
            host = parsed.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            if parsed.port is not None:
                host += f":{parsed.port}"
            return "url", urlunsplit(
                (
                    parsed.scheme.lower(),
                    host,
                    parsed.path,
                    parsed.query,
                    parsed.fragment,
                )
            )
        except ValueError:
            return "", ""
    return "", ""


def finding_values(operation, item):
    kind, key = identifier_key(operation, item)
    return {
        "label": item.get("label", ""),
        "preview": (
            item.get("text", "").strip() or scalar_text(item.get("values", {}))
        )[:300],
        "search_text": "\n".join(
            (
                item.get("label", ""),
                item.get("text", ""),
                scalar_text(item.get("values", {})),
            )
        ),
        "weak": item.get("values", {}).get("Weak plate region") is True,
        "match_kind": kind,
        "match_key": key,
        "match_digest": hashlib.sha256(key.encode()).hexdigest() if key else "",
        "match_version": "identifier-strings.v1" if key else "",
    }


def backfill(apps, schema_editor):
    Run = apps.get_model("core", "AnalysisRun")
    Result = apps.get_model("core", "AnalysisResult")
    Finding = apps.get_model("core", "AnalysisFinding")
    FindingSource = apps.get_model("core", "AnalysisFindingSource")
    alias = schema_editor.connection.alias
    runs = Run.objects.using(alias).filter(equivalence_key="").order_by("pk")
    for run in runs.iterator(chunk_size=100):
        with transaction.atomic(using=alias):
            result = (
                Result.objects.using(alias).filter(run=run, case_id=run.case_id).first()
            )
            if result is not None:
                inputs = {source["id"]: source for source in run.inputs}
                for chunk in batched(
                    enumerate(result.data.get("items", [])), 500, strict=False
                ):
                    findings = [
                        Finding(
                            case_id=run.case_id,
                            result=result,
                            item_index=index,
                            **finding_values(run.operation, item),
                        )
                        for index, item in chunk
                    ]
                    Finding.objects.using(alias).bulk_create(findings, batch_size=500)
                    references = (
                        FindingSource(
                            case_id=run.case_id,
                            finding=finding,
                            source_id=key,
                            kind=inputs[key]["kind"],
                            evidence_id=inputs[key]["evidence_id"],
                            attachment_id=inputs[key].get("attachment_id"),
                            source_sha256=inputs[key]["sha256"],
                        )
                        for finding, (_, item) in zip(findings, chunk, strict=True)
                        for key in dict.fromkeys(item.get("source_ids", []))
                        if key in inputs
                    )
                    for source_chunk in batched(references, 500, strict=False):
                        FindingSource.objects.using(alias).bulk_create(
                            source_chunk, batch_size=500
                        )
            Run.objects.using(alias).filter(pk=run.pk).update(
                equivalence_key=equivalence_key(run)
            )


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("core", "0003_finding_catalogue")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]

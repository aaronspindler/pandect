"""Bound private material in PostgreSQL before hydrating analysis inputs."""

from collections import defaultdict

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Func, IntegerField, Sum, TextField
from django.db.models.functions import Cast, Length

from core.analysis.sources import source_id
from core.evidence.queries import scoped_evidence
from core.evidence.types import COMMUNICATION_KINDS, KINDS, LOCATION_KINDS
from core.models import AnalysisResult
from phone.models import CallEvidenceAttachment


def payload_bytes(rows, *fields):
    """Measure uncompressed payloads in PostgreSQL before loading any into Python."""
    lengths = [
        Func(
            Cast(field, TextField()),
            function="octet_length",
            output_field=IntegerField(),
        )
        for field in fields
    ]
    return rows.aggregate(total=sum(Sum(length, default=0) for length in lengths))[
        "total"
    ]


def check_operation_source_limits(case, operation, parameters, selections):
    from core.analysis.operations import OPERATIONS

    if operation == "text_topics" and parameters.get("_search_generation") == "briefs":
        # The section reader bounds its substring and verifies one source.
        if len(selections) != 1:
            raise ValidationError("A document brief section requires one original.")
        return
    specification = OPERATIONS.get(operation)
    text_ids = (
        parameters.get("text_source_ids", [])
        if operation == "question_synthesis"
        else None
    )
    check_material_size(
        case,
        selections,
        text_limit=specification.max_text_characters if specification else None,
        text_source_ids=[str(value) for value in text_ids]
        if isinstance(text_ids, list)
        else None,
    )


def check_material_size(case, selections, *, text_limit=None, text_source_ids=None):
    """Budget bodies, transcripts and retained result JSON that resolution loads."""
    if not 1 <= len(selections) <= settings.PANDECT_ANALYSIS_MAX_SOURCES:
        raise ValidationError("Choose between 1 and 500 retained sources.")
    by_kind = defaultdict(set)
    attachments = set()

    # Before snapshotting, "id" identifies the original. On a frozen snapshot
    # it identifies a virtual source and "evidence_id" identifies the original.
    def collect(entries):
        for entry in entries:
            kind = entry["kind"]
            if kind not in KINDS:
                raise ValidationError("Choose available sources in this case.")
            by_kind[kind].add(entry.get("evidence_id") or entry["id"])
            if addition := entry.get("attachment_id"):
                attachments.add(addition)
        if (
            max(sum(map(len, by_kind.values())), len(attachments))
            > settings.PANDECT_ANALYSIS_MAX_SOURCES
        ):
            raise ValidationError(
                "Too many contributing originals. Choose fewer source versions."
            )

    limit = settings.PANDECT_ANALYSIS_MAX_BYTES

    def check(total):
        if total > limit:
            raise ValidationError(
                "The retained material exceeds the input limit. Choose fewer sources.",
                code="source_material_limit",
            )

    collect(selections)
    if text_limit is not None:
        check_original_text_size(case, selections, text_limit, text_source_ids)
    result_ids = {entry["result_id"] for entry in selections if entry.get("result_id")}
    total = 0
    if result_ids:
        results = AnalysisResult.objects.filter(
            case=case, run__case=case, pk__in=result_ids
        )
        total = payload_bytes(results, "data", "run__inputs", "run__parameters")
        check(total)
        for entries in results.values_list("run__inputs", flat=True).iterator(
            chunk_size=25
        ):
            collect(entries)
    if attachments:
        total += payload_bytes(
            CallEvidenceAttachment.objects.filter(case=case, pk__in=attachments),
            "transcript",
        )
        check(total)
    for kind, ids in by_kind.items():
        fields = ["description", "source_description"]
        if kind in ("text", "text-message"):
            fields.append("body")
        if kind in COMMUNICATION_KINDS:
            fields.extend(
                (
                    "sender__notes",
                    "recipient__notes",
                    "sender__address",
                    "recipient__address",
                )
            )
        if kind in LOCATION_KINDS:
            fields.append("address")
        if kind == "vehicle-location":
            fields.append("raw_row")
        total += payload_bytes(scoped_evidence(case, kind).filter(pk__in=ids), *fields)
        check(total)


def check_original_text_size(case, selections, limit, text_source_ids):
    """Check ordinary selected text without reading bodies into Python."""
    originals = defaultdict(set)
    attachments = set()
    selected = None if text_source_ids is None else set(text_source_ids)
    for entry in selections:
        if entry.get("result_id"):
            continue
        if "evidence_id" in entry:
            source_key, original_key = entry["id"], entry["evidence_id"]
        else:
            source_key, original_key = source_id(entry), entry["id"]
        if selected is not None and source_key not in selected:
            continue
        if entry["kind"] in ("text", "text-message"):
            originals[entry["kind"]].add(original_key)
        elif addition := entry.get("attachment_id"):
            attachments.add(addition)
    total = 0
    for kind, ids in originals.items():
        total += (
            scoped_evidence(case, kind)
            .filter(pk__in=ids)
            .aggregate(characters=Sum(Length("body"), default=0))["characters"]
        )
    if attachments:
        total += CallEvidenceAttachment.objects.filter(
            case=case, pk__in=attachments
        ).aggregate(characters=Sum(Length("transcript"), default=0))["characters"]
    if total > limit:
        raise ValidationError(
            f"The selected sources exceed {limit:,} characters. "
            "Choose fewer or shorter sources.",
            code="source_material_limit",
        )

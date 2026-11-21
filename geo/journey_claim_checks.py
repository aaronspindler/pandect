"""Verify the case entities and exact retained producers used by a comparison."""

import hashlib
from collections import defaultdict
from copy import copy

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import (
    Func,
    IntegerField,
    Prefetch,
    Sum,
    TextField,
    prefetch_related_objects,
)
from django.db.models.functions import Cast

from core.analysis.errors import AnalysisError
from core.evidence.queries import scoped_evidence
from core.evidence.types import COMMUNICATION_KINDS, FILE_KINDS, KINDS, LOCATION_KINDS
from core.models import AnalysisResult
from geo.analysis.deterministic.journey_claims import (
    claimed_subjects,
    validate_comparison,
)
from geo.models import VehicleLocationImport
from people.models import Person
from phone.models import (
    CallEvidenceAttachment,
    CommunicationContext,
    TextMessageEvidence,
)
from vehicle.models import Vehicle

COMPARISON_EXTRACTIONS = {
    "document": ("native_text", "ocr"),
    "image": ("ocr",),
    "audio": ("transcription",),
    "phone-call": ("transcription",),
    "facetime": ("transcription",),
    **dict.fromkeys(LOCATION_KINDS, ("geocode_locations",)),
}


def check_comparison_inputs(run, sources, case, cache, depth):
    from geo.journey_claim_inputs import validate_dependencies

    try:
        check_source_integrity(case, sources)
        subjects = {"person": set(), "vehicle": set()}
        for claim in run.parameters["claims"]:
            for subject in claimed_subjects(claim):
                kind, value = subject.split(":", 1)
                subjects[kind].add(value)
        for kind, model in (("person", Person), ("vehicle", Vehicle)):
            ids = subjects[kind]
            if ids and model.objects.filter(case=case, pk__in=ids).count() != len(ids):
                raise AnalysisError("source_unavailable")

        for source in sources:
            if not source.derived_result_id:
                continue
            operation = cache.prior_results[source.derived_result_id].run.operation
            if operation not in COMPARISON_EXTRACTIONS.get(source.kind, ()):
                raise AnalysisError("invalid_source_chain")
        validate_dependencies(run, sources, case, cache=cache, depth=depth)
        if cache.with_content:
            validate_comparison(sources, run.parameters, check_size=run._state.adding)
    except ValidationError as error:
        if run._state.adding:
            raise
        raise AnalysisError("source_unavailable") from error
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        raise AnalysisError("invalid_source_chain") from error


def _payload_bytes(rows, *fields):
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


def _check_private_key(case, record):
    directory, _, key = (record.file.name or "").rpartition("/")
    expected = f"evidence/cases/{case.pk}/{record._meta.model_name}"
    if directory != expected or key in ("", ".", "..") or "\\" in key:
        raise AnalysisError("source_unavailable")


def check_source_integrity(case, sources):
    """Recompute structured originals and check availability of their source exports."""
    for source in sources:
        if source.kind in FILE_KINDS:
            _check_private_key(case, source.item)
        if source.attachment and source.attachment.file:
            _check_private_key(case, source.attachment)
    originals = {
        s.item for s in sources if s.kind in (*COMMUNICATION_KINDS, *LOCATION_KINDS)
    }
    records = originals | {s.attachment for s in sources if s.attachment is not None}
    vehicles = [
        s.item
        for s in sources
        if s.kind == "vehicle-location" and s.item.import_batch_id
    ]
    prefetch_related_objects(
        vehicles,
        Prefetch(
            "import_batch",
            queryset=VehicleLocationImport.objects.filter(
                case=case, document__case=case
            )
            .select_related("document")
            .only("timezone", "parser_version", "document__sha256", "document__file"),
        ),
    )
    documents = {row.import_batch.document for row in vehicles}
    messages = [row.pk for row in originals if isinstance(row, TextMessageEvidence)]
    contexts = (
        CommunicationContext.objects.filter(message_id__in=messages)
        .select_related("source_document")
        .only(
            "case_id",
            "source_sha256",
            "source_document__case_id",
            "source_document__sha256",
            "source_document__file",
        )
    )
    for context in contexts:
        document = context.source_document
        if (
            context.case_id != case.pk
            or document.case_id != case.pk
            or context.source_sha256 != document.sha256
        ):
            raise AnalysisError("source_changed")
        documents.add(document)
    for document in documents:
        _check_private_key(case, document)
        try:
            available = document.file.storage.exists(document.file.name)
        except OSError:
            available = False
        if not available:
            raise AnalysisError("source_unavailable")

    # Catalogue readers defer bodies and transcripts. Load only the missing field,
    # in batches, without retaining those private bodies in the catalogue cache.
    remaining = settings.PANDECT_ANALYSIS_MAX_BYTES
    for model, field in (
        (TextMessageEvidence, "body"),
        (CallEvidenceAttachment, "transcript"),
    ):
        deferred = {
            row.pk: row
            for row in records
            if isinstance(row, model) and field in row.get_deferred_fields()
        }
        if deferred:
            values = model.objects.filter(case=case, pk__in=deferred)
            remaining -= _payload_bytes(values, field)
            if remaining < 0:
                raise AnalysisError("source_unavailable")
            for pk, body in values.values_list("pk", field).iterator(chunk_size=25):
                original = deferred.pop(pk)
                record = copy(original)
                setattr(record, field, body)
                _check_manifest(record)
                records.remove(original)
            if deferred:
                raise AnalysisError("source_unavailable")
    for record in records:
        _check_manifest(record)


def _check_manifest(record):
    if hashlib.sha256(record.original_bytes()).hexdigest() != record.sha256:
        raise AnalysisError("source_changed")


def check_material_size(case, selections):
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
                "The retained material exceeds the input limit. Choose fewer sources."
            )

    collect(selections)
    result_ids = {entry["result_id"] for entry in selections if entry.get("result_id")}
    total = 0
    if result_ids:
        results = AnalysisResult.objects.filter(
            case=case, run__case=case, pk__in=result_ids
        )
        total = _payload_bytes(results, "data", "run__inputs", "run__parameters")
        check(total)
        for entries in results.values_list("run__inputs", flat=True).iterator(
            chunk_size=25
        ):
            collect(entries)
    if attachments:
        total += _payload_bytes(
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
        total += _payload_bytes(scoped_evidence(case, kind).filter(pk__in=ids), *fields)
        check(total)

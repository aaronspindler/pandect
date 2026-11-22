"""Database/configuration-only prerequisites; never probe services on page loads."""

from urllib.parse import urlencode

from django.db.models import F, Func, IntegerField, OuterRef, Q, Subquery
from django.urls import reverse

from core.analysis.execution import (
    available_executions,
    configured_executions,
    requires_inference,
)
from core.analysis.operations import get_operation
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, LOCATION_KINDS
from core.models import AnalysisRun, ExtractedPassage
from phone.models import CallEvidenceAttachment

TEXT_OPERATIONS = {
    "claim_comparison",
    "entities",
    "copied_text",
    "text_topics",
    "content_events",
    "style_profiles",
    "style_change",
    "session_summaries",
}
COORDINATE_OPERATIONS = {
    "spatial_groups",
    "proximity",
    "candidate_visits",
    "candidate_trips",
    "movement_routines",
    "colocation",
    "travel_discrepancies",
    "geographic_context",
}
FILE_OPERATIONS = {
    "native_text",
    "image_metadata",
    "audio_metadata",
    "ocr",
    "plate_ocr",
    "transcription",
    "imessage_import",
}
UNSUPPORTED_FORMATS = {
    "unsupported_document_format",
    "encrypted_pdf",
    "unsupported_image_format",
    "animated_image",
    "unsupported_audio_format",
    "unsupported_audio_encoding",
    "unsupported_audio_rate",
}


def configuration_readiness(operation, parameters=None, *, case=None):
    if parameters is None:
        parameters = {"include_summary": True, "semantic_links": True}
    configured = configured_executions(operation, parameters)
    if not configured:
        if operation == "claim_comparison":
            return "Configure private text analysis to compare claims."
        return "A service for this analysis is not configured."
    if case is None:
        return ""
    available = available_executions(case, operation, parameters)
    if operation != "claim_comparison" and requires_inference(operation, parameters):
        if case.ai_backend not in configured:
            return "The case's selected AI backend is unavailable for this method."
        ready = case.ai_backend in available
    else:
        ready = bool(available)
    if ready:
        return ""
    return "This analysis needs a destination approved in Case settings."


def _inspections(case, operation, selected, records, attachments):
    if operation not in FILE_OPERATIONS:
        return {}
    identities = Q(pk__in=[])
    snapshots = set()
    for entry in selected.values():
        item = records.get((entry["kind"], entry["id"]))
        addition = attachments.get(entry.get("attachment_id"))
        if item is None:
            continue
        checksum = addition.sha256 if addition else item.sha256
        identity = (entry["kind"], entry["id"], entry.get("attachment_id"), checksum)
        if identity in snapshots:
            continue
        snapshots.add(identity)
        snapshot = {
            "kind": entry["kind"],
            "evidence_id": entry["id"],
            "sha256": checksum,
        }
        if entry.get("attachment_id"):
            snapshot["attachment_id"] = entry["attachment_id"]
        identities |= Q(inputs__contains=[snapshot])
    identity_fields = (
        "inputs__0__kind",
        "inputs__0__evidence_id",
        "inputs__0__attachment_id",
        "inputs__0__sha256",
    )
    runs = (
        AnalysisRun.objects.filter(
            identities,
            case=case,
            result__case=case,
            operation=operation,
            method_version=get_operation(operation).version,
            status__in=("complete", "partial", "empty", "unsupported"),
        )
        .annotate(
            input_count=Func(
                F("inputs"), function="jsonb_array_length", output_field=IntegerField()
            ),
            unsupported_reason=F("result__data__items__0__values__Status"),
        )
        .filter(input_count=1)
        .order_by(*identity_fields, "-created_at", "-pk")
        .distinct(*identity_fields)
        .values("pk", "inputs", "status", "unsupported_reason")
    )
    # A multi-source run's overall outcome cannot establish one input's format.
    return {
        (
            row["inputs"][0].get("kind"),
            row["inputs"][0].get("evidence_id"),
            row["inputs"][0].get("attachment_id"),
            row["inputs"][0].get("sha256"),
        ): row
        for row in runs
    }


def _prepare_url(case, operation, value):
    return (
        reverse("core:analysis:create", args=[case.pk])
        + "?"
        + urlencode({"operation": operation, "source": value})
    )


def source_readiness(case, operation, choices):
    from core.analysis.form_base import selection

    selected = {value: selection(value) for value, _ in choices}
    records = {}
    for kind in {x["kind"] for x in selected.values()}:
        fields = ["pk", "sha256"]
        if operation == "claim_comparison":
            fields.append("is_archived")
        queryset = scoped_evidence(case, kind)
        if kind in {"text", "text-message"}:
            queryset = queryset.annotate(has_text=Q(body__regex=r"\S"))
        if kind in LOCATION_KINDS:
            fields += ["latitude", "longitude"]
        if kind in CALL_KINDS:
            parent = "phone_call_id" if kind == "phone-call" else "facetime_id"
            queryset = queryset.annotate(
                recording_id=Subquery(
                    CallEvidenceAttachment.objects.filter(
                        case=case, **{parent: OuterRef("pk")}
                    )
                    .exclude(file="")
                    .values("pk")[:1]
                )
            )
        for item in queryset.filter(
            pk__in=[x["id"] for x in selected.values() if x["kind"] == kind]
        ).only(*fields):
            records[(kind, str(item.pk))] = item
    attachments = {
        str(x.pk): x
        for x in CallEvidenceAttachment.objects.filter(
            case=case,
            pk__in=[
                x["attachment_id"] for x in selected.values() if x.get("attachment_id")
            ],
        )
        .annotate(has_text=Q(transcript__regex=r"\S"))
        .only(
            "pk",
            "phone_call_id",
            "facetime_id",
            "sha256",
            "file",
        )
    }
    extracted = {
        (str(r), kind, str(e), str(a) if a else None, checksum)
        for r, kind, e, a, checksum in ExtractedPassage.objects.filter(
            case=case,
            result__case=case,
            result__run__case=case,
            result__run__status__in=("complete", "partial"),
            result_id__in=[
                x["result_id"] for x in selected.values() if x.get("result_id")
            ],
            text__regex=r"\S",
        )
        .values_list(
            "result_id", "kind", "evidence_id", "attachment_id", "evidence_sha256"
        )
        .distinct()
    }
    text_versions = {}
    supplied_versions = {}
    for value, entry in selected.items():
        item = records.get((entry["kind"], entry["id"]))
        addition = attachments.get(entry.get("attachment_id"))
        if (
            item
            and addition
            and addition.has_text
            and entry["kind"] in CALL_KINDS
            and str(
                addition.phone_call_id
                if entry["kind"] == "phone-call"
                else addition.facetime_id
            )
            == entry["id"]
            and not entry.get("result_id")
        ):
            supplied_versions.setdefault((entry["kind"], entry["id"]), value)
        if (
            item
            and (
                entry.get("result_id"),
                entry["kind"],
                entry["id"],
                entry.get("attachment_id"),
                item.sha256,
            )
            in extracted
        ):
            text_versions.setdefault(
                (entry["kind"], entry["id"], entry.get("attachment_id")), value
            )
    inspections = _inspections(case, operation, selected, records, attachments)
    result = {}
    for value, entry in selected.items():
        kind = entry["kind"]
        item = records.get((kind, entry["id"]))
        addition = attachments.get(entry.get("attachment_id"))
        valid_attachment = not entry.get("attachment_id") or (
            addition is not None
            and kind in CALL_KINDS
            and str(
                addition.phone_call_id if kind == "phone-call" else addition.facetime_id
            )
            == entry["id"]
        )
        state, url, action_label, blocking = "Ready", "", "Prepare source", False
        if item is None or not valid_attachment:
            state, blocking = "Source unavailable", True
        elif operation == "claim_comparison" and item.is_archived:
            state, blocking = "Archived source", True
        elif operation in TEXT_OPERATIONS:
            identity = (kind, entry["id"], entry.get("attachment_id"))
            has_text = (
                (entry["result_id"], *identity, item.sha256) in extracted
                if entry.get("result_id")
                else getattr(addition or item, "has_text", False)
            )
            if not has_text:
                blocking = True
                if identity in text_versions:
                    state, action_label = (
                        "Choose extracted text",
                        "Choose exact extraction",
                    )
                    url = _prepare_url(case, operation, text_versions[identity])
                elif kind in {"text", "text-message"}:
                    state = "No usable text"
                elif kind in CALL_KINDS and not addition:
                    supplied = supplied_versions.get((kind, entry["id"]))
                    if supplied:
                        state, action_label = (
                            "Choose supplied transcript",
                            "Choose transcript",
                        )
                        url = _prepare_url(case, operation, supplied)
                    elif item.recording_id:
                        state, action_label = "Needs transcript", "Transcribe recording"
                        url = _prepare_url(
                            case,
                            "transcription",
                            f"{kind}:{item.pk}:{item.recording_id}",
                        )
                    else:
                        state, action_label = (
                            "Needs transcript",
                            "Add transcript or audio",
                        )
                        url = reverse(
                            "phone:add_attachment", args=[case.pk, kind, item.pk]
                        )
                else:
                    state = (
                        "Needs transcript"
                        if kind in {"audio", *CALL_KINDS}
                        else "Needs extraction"
                    )
                    action = (
                        "transcription"
                        if state == "Needs transcript"
                        else "ocr"
                        if kind == "image"
                        else "native_text"
                    )
                    original = (
                        kind
                        + ":"
                        + entry["id"]
                        + (
                            ":" + entry["attachment_id"]
                            if entry.get("attachment_id")
                            else ""
                        )
                    )
                    url = _prepare_url(case, action, original)
        elif operation in COORDINATE_OPERATIONS and kind in LOCATION_KINDS:
            if item.latitude is None and entry.get("geocode_candidate") is None:
                state, blocking = "Needs accepted location", True
                url = _prepare_url(case, "geocode_locations", value)
        elif operation in FILE_OPERATIONS:
            checksum = addition.sha256 if addition else item.sha256
            latest = inspections.get(
                (kind, entry["id"], entry.get("attachment_id"), checksum)
            )
            state = "Not yet inspected"
            if latest and latest["status"] in {"complete", "partial", "empty"}:
                state = "Ready"
            elif (
                latest
                and isinstance(latest["unsupported_reason"], str)
                and latest["unsupported_reason"] in UNSUPPORTED_FORMATS
            ):
                state, blocking = "Unsupported format", True
                url = reverse("core:evidence:detail", args=[case.pk, kind, item.pk])
                action_label = "Inspect original"
            elif latest:
                state, action_label = "Needs review", "Review previous attempt"
                url = reverse("core:analysis:detail", args=[case.pk, latest["pk"]])
        result[value] = {
            "label": state,
            "url": url,
            "action_label": action_label,
            "blocking": blocking,
        }
    return result

"""Freeze bounded selections of originals, additions and exact derived versions."""

from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.analysis.source_limits import check_material_size
from core.analysis.source_resolution import SourceResolutionCache, resolve_snapshots
from core.analysis.sources import source_id
from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS, KINDS
from core.models import AnalysisResult
from phone.models import CallEvidenceAttachment


def selection_uuid(value):
    try:
        return str(UUID(str(value)))
    except ValueError, TypeError, AttributeError:
        raise ValidationError("Choose a valid retained source.") from None


def _attachment(case, kind, item, attachment_id):
    if not attachment_id:
        return None
    if kind not in CALL_KINDS:
        raise Http404("Source not found.")
    parent = "phone_call" if kind == "phone-call" else "facetime"
    try:
        return CallEvidenceAttachment.objects.only("pk", "sha256").get(
            case=case, pk=attachment_id, **{parent: item}
        )
    except CallEvidenceAttachment.DoesNotExist:
        raise Http404("Source not found.") from None


def snapshot_source_references(
    case, selections, *, allowed_kinds=None, active_only=True, direct_messages=False
):
    """Freeze source identities without fetching retained bodies or transcripts."""
    if not 1 <= len(selections) <= settings.PANDECT_ANALYSIS_MAX_SOURCES:
        raise ValidationError("Select between 1 and 500 retained sources.")
    snapshots = []
    seen = set()
    for choice in selections:
        kind = choice["kind"]
        if kind not in (allowed_kinds if allowed_kinds is not None else KINDS):
            raise ValidationError(
                "This evidence type is not supported by the selected analysis."
            )
        try:
            fields = ("pk", "is_archived", "sha256")
            if kind in CALL_KINDS:
                fields += ("import_row_id", "sender_id", "recipient_id")
            if direct_messages and kind == "text-message":
                fields += ("recipient_id",)
            item = (
                scoped_evidence(case, kind)
                .only(*fields)
                .get(pk=selection_uuid(choice["id"]))
            )
        except KINDS[kind].DoesNotExist:
            raise Http404("Evidence not found.") from None
        if active_only and item.is_archived:
            raise AnalysisError("source_archived")
        if direct_messages and kind == "text-message" and item.recipient_id is None:
            raise AnalysisError("direct_messages_required")
        attachment_id = (
            selection_uuid(choice["attachment_id"])
            if choice.get("attachment_id")
            else None
        )
        attachment = _attachment(case, kind, item, attachment_id)
        result_id = (
            selection_uuid(choice["result_id"]) if choice.get("result_id") else None
        )
        candidate = choice.get("geocode_candidate")
        if candidate is not None and type(candidate) is not int:
            raise AnalysisError("ambiguous_geocode")
        identity_candidate = None
        review_id = None
        if result_id:
            prior = (
                AnalysisResult.objects.filter(pk=result_id, case=case, run__case=case)
                .select_related("run")
                .defer("data", "provenance")
                .first()
            )
            if prior is None:
                raise AnalysisError("source_unavailable")
            if prior.run.operation == "geocode_locations":
                candidates = [
                    (index, finding)
                    for index, finding in enumerate(prior.data.get("items", []))
                    if any(
                        x["id"] in finding.get("source_ids", [])
                        for x in prior.run.inputs
                        if x.get("kind") == kind
                        and x.get("evidence_id") == str(item.pk)
                    )
                    and finding.get("values", {}).get("latitude") is not None
                ]
                candidate_index = choice.get("geocode_candidate")
                if candidate_index is None and len(candidates) == 1:
                    candidate_index = candidates[0][0]
                identity_candidate = candidate_index
                accepted = prior.reviews.filter(
                    case=case, item_index=candidate_index
                ).first()
                review_id = (
                    str(accepted.pk)
                    if accepted and accepted.decision == "accepted"
                    else None
                )
        identity = (kind, str(item.pk), attachment_id, result_id, identity_candidate)
        if identity in seen:
            raise ValidationError("Choose each source once.")
        seen.add(identity)
        snapshots.append(
            {
                "geocode_candidate": choice.get("geocode_candidate"),
                "id": source_id(choice),
                "kind": kind,
                "evidence_id": str(item.pk),
                "attachment_id": attachment_id,
                "result_id": result_id,
                "review_id": review_id,
                "sha256": attachment.sha256 if attachment else item.sha256,
                **(
                    {
                        "call_person_ids": [
                            str(person) if person is not None else None
                            for person in (item.sender_id, item.recipient_id)
                        ]
                    }
                    if kind in CALL_KINDS and item.import_row_id
                    else {}
                ),
            }
        )
    return snapshots


def snapshot_and_resolve_sources(
    case, selections, *, allowed_kinds=None, active_only=True
):
    """Freeze a bounded selection and return its verified source versions."""
    check_material_size(case, selections)
    snapshots = snapshot_source_references(
        case, selections, allowed_kinds=allowed_kinds, active_only=active_only
    )
    return snapshots, resolve_snapshots(
        snapshots, case, cache=SourceResolutionCache(case, active_only=active_only)
    )


def snapshot_sources(case, selections, *, allowed_kinds=None):
    """Validate and freeze a bounded selection for runs or saved collections."""
    snapshots, _ = snapshot_and_resolve_sources(
        case, selections, allowed_kinds=allowed_kinds, active_only=False
    )
    return snapshots

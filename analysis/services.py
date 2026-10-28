"""Current authorization, immutable input selection and duplicate-safe publication."""

import base64
import hashlib
import inspect
import json
import math
from uuid import UUID, uuid4

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from cases.permissions import get_case
from evidence.models import CallEvidenceAttachment
from evidence.queries import get_evidence
from evidence.types import CALL_KINDS

from .errors import AnalysisError
from .geocoding import GeocodingError
from .models import (
    AnalysisArtifact,
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
    ExtractedPassage,
)
from .operations import get_operation
from .sources import Source, source_id

FINISHED = set(AnalysisRun.Status.values) - {"queued", "running"}


def _uuid(value):
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
        return CallEvidenceAttachment.objects.get(
            case=case, pk=attachment_id, **{parent: item}
        )
    except CallEvidenceAttachment.DoesNotExist:
        raise Http404("Source not found.") from None


def resolve_inputs(run, case, *, require_current_review=True):
    sources = []
    for selected in run.inputs:
        try:
            item = get_evidence(case, selected["kind"], selected["evidence_id"])
            attachment = _attachment(
                case, selected["kind"], item, selected.get("attachment_id")
            )
        except Http404, ValueError, KeyError, ValidationError:
            raise AnalysisError("source_unavailable") from None
        checksum = attachment.sha256 if attachment else item.sha256
        original_file = attachment.file if attachment else getattr(item, "file", None)
        if original_file:
            try:
                available = original_file.storage.exists(original_file.name)
            except OSError:
                available = False
            if not available:
                raise AnalysisError("source_unavailable")
        if checksum != selected["sha256"]:
            raise AnalysisError("source_changed")
        if (
            selected["kind"] == "text"
            and hashlib.sha256(item.body.encode()).hexdigest() != checksum
        ):
            raise AnalysisError("source_changed")
        text = None
        coordinates = None
        derived_result_id = derived_run_id = None
        segments = []
        if selected.get("result_id"):
            try:
                prior = AnalysisResult.objects.select_related("run").get(
                    pk=selected["result_id"],
                    case=case,
                    run__case=case,
                    run__status__in=("complete", "partial", "empty"),
                )
            except AnalysisResult.DoesNotExist:
                raise AnalysisError("source_unavailable") from None
            matching = {
                x["id"]
                for x in prior.run.inputs
                if x.get("kind") == selected["kind"]
                and x.get("evidence_id") == str(item.pk)
                and x.get("sha256") == checksum
                and x.get("attachment_id") == selected.get("attachment_id")
            }
            if not matching:
                raise AnalysisError("source_changed")
            # Revalidate the prior source selection as well; do not trust JSON IDs.
            if prior.run.pk == run.pk:
                raise AnalysisError("invalid_source_chain")
            for original in prior.run.inputs:
                old = get_evidence(case, original["kind"], original["evidence_id"])
                old_attachment = _attachment(
                    case, original["kind"], old, original.get("attachment_id")
                )
                if (
                    old_attachment.sha256 if old_attachment else old.sha256
                ) != original["sha256"]:
                    raise AnalysisError("source_changed")
            pieces = []
            cursor = 0
            for index, piece in enumerate(prior.data.get("items", [])):
                if not matching.intersection(piece.get("source_ids", [])):
                    continue
                value = piece.get("text", "")
                if pieces:
                    cursor += 1  # Exact separator in the retained derived text view.
                segments.append(
                    {
                        "start": cursor,
                        "end": cursor + len(value),
                        "finding_index": index,
                        "anchors": piece.get("anchors", []),
                    }
                )
                cursor += len(value)
                pieces.append(piece)
            text = "\n".join(x.get("text", "") for x in pieces)
            derived_result_id, derived_run_id = str(prior.pk), str(prior.run_id)
            if prior.run.operation == "geocode_locations":
                candidate_index = selected.get("geocode_candidate")
                candidates = [
                    (index, x)
                    for index, x in enumerate(prior.data.get("items", []))
                    if matching.intersection(x.get("source_ids", []))
                    and x.get("values", {}).get("latitude") is not None
                ]
                if isinstance(candidate_index, bool):
                    raise AnalysisError("ambiguous_geocode")
                if candidate_index is None and len(candidates) == 1:
                    candidate_index = candidates[0][0]
                candidates = [x for index, x in candidates if index == candidate_index]
                if len(candidates) != 1:
                    raise AnalysisError("ambiguous_geocode")
                review = prior.reviews.filter(item_index=candidate_index).first()
                if not require_current_review:
                    review = prior.reviews.filter(
                        pk=selected.get("review_id"), item_index=candidate_index
                    ).first()
                if not review or review.decision != "accepted":
                    raise AnalysisError("geocode_review_required")
                values = candidates[0]["values"]
                coordinates = (float(values["latitude"]), float(values["longitude"]))
        sources.append(
            Source(
                selected["id"],
                selected["kind"],
                item,
                attachment,
                text,
                coordinates,
                derived_result_id,
                derived_run_id,
                tuple(segments),
            )
        )
    return sources


def authorize_run(run, *, lock=False, allow_finished=False):
    actor = get_user_model().objects.get(pk=run.requested_by_id)
    case = get_case(actor, run.case_id, edit=True, lock=lock)
    if not EmailAddress.objects.filter(user=actor, verified=True).exists():
        raise PermissionDenied("Verify your email before processing.")
    current = AnalysisRun.objects.get(pk=run.pk)
    if current.status == "cancelled" or (
        not allow_finished and current.status in FINISHED
    ):
        raise AnalysisError("processing_cancelled")
    resolve_inputs(run, case)
    return case


@transaction.atomic
def start_run(
    actor,
    case_id,
    operation,
    selections,
    *,
    parameters=None,
    submission_id=None,
    external_processing=False,
    previous_run=None,
):
    case = get_case(actor, case_id, edit=True, lock=True)
    specification = get_operation(operation)
    parameters = {} if parameters is None else parameters
    try:
        valid_parameters = (
            isinstance(parameters, dict)
            and len(json.dumps(parameters, allow_nan=False).encode()) <= 32768
        )
    except ValueError, TypeError:
        valid_parameters = False
    if not valid_parameters:
        raise ValidationError("Analysis settings exceed the supported limit.")
    if specification.external and not external_processing:
        raise ValidationError("Approve external processing for this operation.")
    if not 1 <= len(selections) <= settings.PANDECT_ANALYSIS_MAX_SOURCES:
        raise ValidationError("Select between 1 and 500 retained sources.")
    submission_id = UUID(_uuid(submission_id or uuid4()))
    snapshots = []
    seen = set()
    for choice in selections:
        kind = choice["kind"]
        if kind not in specification.kinds:
            raise ValidationError(
                "This evidence type is not supported by the selected analysis."
            )
        item = get_evidence(case, kind, _uuid(choice["id"]))
        attachment_id = (
            _uuid(choice["attachment_id"]) if choice.get("attachment_id") else None
        )
        attachment = _attachment(case, kind, item, attachment_id)
        result_id = _uuid(choice["result_id"]) if choice.get("result_id") else None
        identity = (kind, str(item.pk), attachment_id, result_id)
        if identity in seen:
            raise ValidationError("Choose each source once.")
        seen.add(identity)
        review_id = None
        if result_id:
            prior = (
                AnalysisResult.objects.filter(pk=result_id, case=case, run__case=case)
                .select_related("run")
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
                accepted = prior.reviews.filter(item_index=candidate_index).first()
                review_id = (
                    str(accepted.pk)
                    if accepted and accepted.decision == "accepted"
                    else None
                )
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
            }
        )
    existing = AnalysisRun.objects.filter(submission_id=submission_id).first()
    if existing:

        def comparable(values):
            return [{k: v for k, v in x.items() if k != "id"} for x in values]

        if (
            existing.case_id != case.pk
            or existing.requested_by_id != actor.pk
            or existing.operation != operation
            or existing.parameters != parameters
            or comparable(existing.inputs) != comparable(snapshots)
            or existing.external_processing != external_processing
            or existing.previous_run_id != (previous_run.pk if previous_run else None)
        ):
            raise ValidationError(
                "This submission was already used for different analysis settings."
            )
        return existing, False
    if previous_run and previous_run.case_id != case.pk:
        raise Http404("Processing not found.")
    run = AnalysisRun(
        case=case,
        requested_by=actor,
        operation=operation,
        method_version=specification.version,
        parameters=parameters,
        inputs=snapshots,
        submission_id=submission_id,
        external_processing=external_processing,
        previous_run=previous_run,
    )
    resolve_inputs(run, case)
    run.save()
    from .tasks import process_analysis

    process_analysis.enqueue(str(run.pk))
    return run, True


def validate_output(data, sources):
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        raise AnalysisError("invalid_result")
    if data.get("outcome", "complete") not in {
        "complete",
        "partial",
        "empty",
        "insufficient",
        "unsupported",
    }:
        raise AnalysisError("invalid_result")
    if len(json.dumps(data, allow_nan=False).encode()) > 4 * 1024 * 1024:
        raise AnalysisError("result_limit")
    source_map = {str(x.id): x for x in sources}
    items = data.get("items", [])
    if not isinstance(items, list) or len(items) > 10000:
        raise AnalysisError("invalid_result")
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("label"), str)
            or not isinstance(item.get("text", ""), str)
        ):
            raise AnalysisError("invalid_result")
        refs = item.get("source_ids", [])
        if not refs or not set(refs) <= source_map.keys():
            raise AnalysisError("invalid_anchor")
        for anchor in item.get("anchors", []):
            if not isinstance(anchor, dict) or anchor.get("source_id") not in refs:
                raise AnalysisError("invalid_anchor")
            source = source_map[anchor["source_id"]]
            if "start" in anchor or "end" in anchor:
                start, end = anchor.get("start"), anchor.get("end")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start <= end <= len(source.text)
                ):
                    raise AnalysisError("invalid_anchor")
            if "page" in anchor and (
                type(anchor["page"]) is not int or not 1 <= anchor["page"] <= 100
            ):
                raise AnalysisError("invalid_anchor")
            if "region" in anchor:
                region = anchor["region"]
                if (
                    not isinstance(region, list)
                    or len(region) != 4
                    or not all(
                        type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1
                        for x in region
                    )
                    or region[0] >= region[2]
                    or region[1] >= region[3]
                ):
                    raise AnalysisError("invalid_anchor")
            if "start_seconds" in anchor or "end_seconds" in anchor:
                start, end = anchor.get("start_seconds"), anchor.get("end_seconds")
                if (
                    not all(
                        type(x) in (int, float) and math.isfinite(x)
                        for x in (start, end)
                    )
                    or not 0 <= start <= end
                ):
                    raise AnalysisError("invalid_anchor")
    return data


def process_run(run_id):
    with transaction.atomic():
        try:
            run = AnalysisRun.objects.select_for_update().get(pk=run_id)
        except AnalysisRun.DoesNotExist:
            return "unavailable"
        if run.status != "queued":
            return run.status
        run.status = "running"
        run.attempts += 1
        run.started_at = timezone.now()
        run.progress = 10
        run.save(update_fields=["status", "attempts", "started_at", "progress"])
    stored_files = []
    try:
        case = authorize_run(run)
        sources = resolve_inputs(run, case)
        operation = get_operation(run.operation)
        if operation.version != run.method_version:
            raise AnalysisError("processing_version_unavailable")
        from .gateway import GatewayClient

        gateway = GatewayClient(run, lambda **kw: authorize_run(run, **kw))
        kwargs = {}
        signature = inspect.signature(operation.function)
        if "gateway" in signature.parameters:
            kwargs["gateway"] = gateway
        if "authorize" in signature.parameters:
            kwargs["authorize"] = lambda: authorize_run(run)
        if "infer" in signature.parameters and run.external_processing:
            kwargs["infer"] = gateway.infer
        parameters = dict(run.parameters)
        if run.operation == "geocode_locations":
            parameters["external_processing"] = run.external_processing
        data = operation.function(sources, parameters, **kwargs)
        artifacts = data.pop("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 500:
            raise AnalysisError("invalid_preview")
        data = validate_output(data, sources)
        with transaction.atomic():
            case = authorize_run(run, lock=True)
            current = AnalysisRun.objects.select_for_update().get(pk=run.pk)
            if current.status != "running":
                return current.status
            if run.operation == "conversation_import":
                from .communication_imports import apply_import

                apply_import(run, data, sources)
            result = AnalysisResult.objects.create(
                case=case,
                run=run,
                data=data,
                provenance={
                    "method_version": run.method_version,
                    "operation": run.operation,
                    "external_processing": run.external_processing,
                    "source_inputs": run.inputs,
                },
            )
            total_preview_bytes = 0
            for preview in artifacts:
                try:
                    raw = base64.b64decode(preview["data_base64"], validate=True)
                    total_preview_bytes += len(raw)
                    if (
                        preview["source_id"] not in {s.id for s in sources}
                        or preview["content_type"] != "image/jpeg"
                        or not raw.startswith(b"\xff\xd8\xff")
                        or total_preview_bytes > 2 * 1024 * 1024
                    ):
                        raise AnalysisError("invalid_preview")
                    artifact = AnalysisArtifact(
                        case=case,
                        result=result,
                        source_id=preview["source_id"],
                        sha256=hashlib.sha256(raw).hexdigest(),
                        width=preview["width"],
                        height=preview["height"],
                    )
                    artifact.file.save("preview.jpg", ContentFile(raw), save=False)
                    stored_files.append((artifact.file.storage, artifact.file.name))
                    artifact.save()
                except ValueError, KeyError, TypeError:
                    raise AnalysisError("invalid_preview") from None
            if run.operation in {"native_text", "ocr", "transcription"}:
                for item_index, item in enumerate(data.get("items", [])):
                    if not item.get("text"):
                        continue
                    for source_id in item.get("source_ids", []):
                        source = next(x for x in sources if x.id == source_id)
                        ExtractedPassage.objects.create(
                            case=case,
                            result=result,
                            kind=source.kind,
                            evidence_id=source.item.pk,
                            evidence_sha256=source.item.sha256,
                            attachment_id=source.attachment.pk
                            if source.attachment
                            else None,
                            text=item["text"],
                            item_index=item_index,
                            anchors=item.get("anchors", []),
                        )
            current.status = data.get("outcome", "complete")
            current.progress = 100
            current.finished_at = timezone.now()
            current.save(update_fields=["status", "progress", "finished_at"])
            return str(result.pk)
    except PermissionDenied, Http404:
        code = "access_removed"
    except (AnalysisError, GeocodingError) as error:
        code = error.code
    except Exception:
        # Parser/provider strings can contain private source contents.
        code = "processing_failed"
    from evidence.signals import delete_original

    for storage, name in stored_files:
        delete_original(storage, name)
    AnalysisRun.objects.filter(pk=run_id, status="running").update(
        status="failed", error_code=code, finished_at=timezone.now()
    )
    return code


@transaction.atomic
def cancel_run(actor, case_id, run_id):
    case = get_case(actor, case_id, edit=True, lock=True)
    run = AnalysisRun.objects.select_for_update().filter(case=case, pk=run_id).first()
    if run is None:
        raise Http404("Processing not found.")
    if run.status not in FINISHED:
        run.status = "cancelled"
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at"])
    return run


@transaction.atomic
def review_result(actor, case_id, result_id, decision, comment="", item_index=None):
    if item_index is not None and (type(item_index) is not int or item_index < 0):
        raise ValidationError("Choose a valid finding index.")
    case = get_case(actor, case_id, edit=True, lock=True)
    try:
        result = AnalysisResult.objects.select_related("run").get(
            case=case, run__case=case, pk=result_id
        )
    except AnalysisResult.DoesNotExist:
        raise Http404("Result not found.") from None
    resolve_inputs(result.run, case, require_current_review=False)
    return AnalysisReview.objects.create(
        case=case,
        result=result,
        reviewed_by=actor,
        decision=decision,
        comment=comment,
        item_index=item_index,
    )

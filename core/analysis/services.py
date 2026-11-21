"""Current authorization, immutable input selection and duplicate-safe publication."""

import hashlib
import json
import math
from itertools import batched
from uuid import UUID, uuid4

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import CharField, F, Func, OuterRef, Q, Subquery, Value
from django.http import Http404
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from cases.permissions import get_case
from core.analysis.artifacts import (
    MAX_PREVIEW_TOTAL_BYTES,
    MAX_REFERENCE_TOTAL_BYTES,
    decode_artifact,
)
from core.analysis.diagnostics import log_analysis_failure
from core.analysis.errors import AnalysisError
from core.analysis.execution import (
    inference_configured,
    requires_inference,
    retry_execution_confirmation,
    snapshot_execution,
    validate_execution,
)
from core.analysis.operations import get_operation
from core.analysis.sources import Source, source_id
from core.evidence.queries import get_evidence, scoped_evidence
from core.evidence.types import CALL_KINDS, KINDS
from core.models import (
    AnalysisArtifact,
    AnalysisResult,
    AnalysisReview,
    AnalysisRun,
    ExtractedPassage,
)
from geo.analysis.deterministic.locations import coordinate
from geo.analysis.external.geocoding import GeocodingError
from phone.models import CallEvidenceAttachment

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


class SourceResolutionCache:
    """Request-local source lookups; display readers need no complete text bodies."""

    def __init__(self, case, *, with_content=True):
        self.case = case
        self.with_content = with_content
        self.originals = {}
        self.attachments = {}
        self.prior_results = {}
        self.validated_originals = set()
        self.finding_results = {}
        self.finding_dependency_heights = {}
        self.finding_dependency_stack = set()

    def prepare(self, selections):
        by_kind = {}
        for selected in selections:
            if (
                not isinstance(selected, dict)
                or not selected.get("kind")
                or not selected.get("evidence_id")
            ):
                continue
            key = (selected["kind"], selected["evidence_id"])
            if key not in self.originals:
                by_kind.setdefault(key[0], set()).add(key[1])
        for kind, ids in by_kind.items():
            if kind not in KINDS:
                continue
            for chunk in batched(ids, 500, strict=False):
                query = scoped_evidence(self.case, kind).filter(pk__in=chunk)
                if kind in {"text-message", "phone-call", "facetime"}:
                    query = query.select_related("sender", "recipient")
                if not self.with_content and kind in {"text", "text-message"}:
                    query = query.defer("body")
                    if kind == "text":
                        query = query.annotate(
                            content_sha256=Func(
                                Func(
                                    Func(
                                        F("body"), Value("UTF8"), function="convert_to"
                                    ),
                                    function="sha256",
                                ),
                                Value("hex"),
                                function="encode",
                                output_field=CharField(),
                            )
                        )
                self.originals.update({(kind, key): None for key in chunk})
                self.originals.update({(kind, str(item.pk)): item for item in query})
        additions = {
            selected["attachment_id"]
            for selected in selections
            if isinstance(selected, dict)
            and selected.get("attachment_id")
            and selected["attachment_id"] not in self.attachments
        }
        for chunk in batched(additions, 500, strict=False):
            query = CallEvidenceAttachment.objects.filter(case=self.case, pk__in=chunk)
            if not self.with_content:
                query = query.defer("transcript")
            self.attachments.update(dict.fromkeys(chunk))
            self.attachments.update({str(item.pk): item for item in query})

    def original(self, selected):
        item = self.originals.get((selected["kind"], selected["evidence_id"]))
        if item is None:
            raise Http404("Source not found.")
        attachment_id = selected.get("attachment_id")
        if not attachment_id:
            return item, None
        attachment = self.attachments.get(attachment_id)
        parent = {"phone-call": "phone_call_id", "facetime": "facetime_id"}.get(
            selected["kind"]
        )
        if not parent or attachment is None or getattr(attachment, parent) != item.pk:
            raise Http404("Source not found.")
        return item, attachment


def resolve_inputs(
    run, case, *, require_current_review=True, cache=None, finding_depth=0
):
    cache = cache or SourceResolutionCache(case)
    sources = resolve_snapshots(
        run.inputs,
        case,
        run_id=run.pk,
        require_current_review=require_current_review,
        cache=cache,
    )
    if run.operation == "question_synthesis":
        from text.analysis.synthesis import validate_references

        validate_references(run, sources, case, cache=cache, depth=finding_depth)
    return sources


def resolve_snapshots(
    inputs, case, *, run_id=None, require_current_review=True, cache=None
):
    """Resolve exact retained references without constructing a fictitious run."""
    sources = []
    cache = cache or SourceResolutionCache(case)
    if cache.case.pk != case.pk:
        raise AnalysisError("source_unavailable")
    try:
        cache.prepare(inputs)
    except Http404, ValueError, KeyError, ValidationError:
        raise AnalysisError("source_unavailable") from None
    original = cache.original
    prior_results = cache.prior_results
    validated_originals = cache.validated_originals

    for selected in inputs:
        try:
            item, attachment = original(selected)
        except Http404, ValueError, KeyError, ValidationError:
            raise AnalysisError("source_unavailable") from None
        checksum = attachment.sha256 if attachment else item.sha256
        if checksum != selected["sha256"]:
            raise AnalysisError("source_changed")
        identity = (
            selected["kind"],
            selected["evidence_id"],
            selected.get("attachment_id"),
        )
        if identity not in validated_originals:
            original_file = (
                attachment.file if attachment else getattr(item, "file", None)
            )
            if original_file:
                try:
                    available = original_file.storage.exists(original_file.name)
                except OSError:
                    available = False
                if not available:
                    raise AnalysisError("source_unavailable")
            if (
                selected["kind"] == "text"
                and (
                    getattr(item, "content_sha256", None)
                    or hashlib.sha256(item.body.encode()).hexdigest()
                )
                != checksum
            ):
                raise AnalysisError("source_changed")
            validated_originals.add(identity)
        text = None
        coordinates = None
        derived_result_id = derived_run_id = None
        candidate_index = None
        segments = []
        if selected.get("result_id"):
            result_id = selected["result_id"]
            if result_id not in prior_results:
                try:
                    prior = (
                        AnalysisResult.objects.select_related("run")
                        .defer(
                            "provenance", *(["data"] if not cache.with_content else [])
                        )
                        .get(
                            pk=result_id,
                            case=case,
                            run__case=case,
                            run__status__in=("complete", "partial", "empty"),
                        )
                    )
                    if prior.run.pk == run_id:
                        raise AnalysisError("invalid_source_chain")
                    # Check each retained source once per resolution, including
                    # sources not selected by this downstream run.
                    cache.prepare(prior.run.inputs)
                    for previous in prior.run.inputs:
                        old, old_attachment = original(previous)
                        if (
                            old_attachment.sha256 if old_attachment else old.sha256
                        ) != previous["sha256"]:
                            raise AnalysisError("source_changed")
                except (
                    AnalysisResult.DoesNotExist,
                    Http404,
                    ValueError,
                    KeyError,
                    ValidationError,
                ):
                    raise AnalysisError("source_unavailable") from None
                prior_results[result_id] = prior
            prior = prior_results[result_id]
            if prior.run.pk == run_id:
                raise AnalysisError("invalid_source_chain")
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
            pieces = []
            cursor = 0
            for index, piece in enumerate(
                prior.data.get("items", []) if cache.with_content else []
            ):
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
                reviews = prior.reviews.filter(case=case, item_index=candidate_index)
                if not require_current_review:
                    reviews = reviews.filter(pk=selected.get("review_id"))
                review = reviews.first()
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
                geocode_candidate=candidate_index,
            )
        )
    return sources


def require_current_geocode_reviews(run, sources):
    """Check current acceptance after historical inputs have been resolved."""
    geocodes = {
        source.id for source in sources if source.derived_coordinates is not None
    }
    review_ids = {
        selected["review_id"] for selected in run.inputs if selected["id"] in geocodes
    }
    if not review_ids:
        return
    latest = AnalysisReview.objects.filter(
        case_id=run.case_id,
        result_id=OuterRef("result_id"),
        item_index=OuterRef("item_index"),
    ).order_by("-created_at", "id")
    accepted = (
        AnalysisReview.objects.filter(
            pk__in=review_ids,
            case_id=run.case_id,
            result__case_id=run.case_id,
            result__run__case_id=run.case_id,
        )
        .annotate(current_decision=Subquery(latest.values("decision")[:1]))
        .filter(current_decision=AnalysisReview.Decision.ACCEPTED)
        .count()
    )
    if accepted != len(review_ids):
        raise AnalysisError("geocode_review_required")


def _authorize_run(run, *, lock=False):
    actor = get_user_model().objects.get(pk=run.requested_by_id)
    case = get_case(actor, run.case_id, edit=True, lock=lock)
    if not EmailAddress.objects.filter(user=actor, verified=True).exists():
        raise PermissionDenied("Verify your email before processing.")
    current = AnalysisRun.objects.get(pk=run.pk)
    if current.status in FINISHED:
        raise AnalysisError("processing_cancelled")
    return case


def authorize_run(run, *, lock=False):
    case = _authorize_run(run, lock=lock)
    validate_execution(run)
    resolve_inputs(run, case)
    return case


def snapshot_sources(case, selections, *, allowed_kinds=None):
    """Validate and freeze a bounded source selection for runs or saved collections."""
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
        item = get_evidence(case, kind, _uuid(choice["id"]))
        attachment_id = (
            _uuid(choice["attachment_id"]) if choice.get("attachment_id") else None
        )
        attachment = _attachment(case, kind, item, attachment_id)
        result_id = _uuid(choice["result_id"]) if choice.get("result_id") else None
        candidate = choice.get("geocode_candidate")
        if candidate is not None and type(candidate) is not int:
            raise AnalysisError("ambiguous_geocode")
        identity_candidate = None
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
            }
        )
    resolve_snapshots(snapshots, case)
    return snapshots


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
    retry_confirmation="",
    batch=None,
    approved_execution=None,
    require_configured_inference=False,
):
    case = get_case(actor, case_id, edit=True, lock=True)
    if previous_run:
        if previous_run.case_id != case.pk:
            raise Http404("Processing not found.")
        if batch is None:
            batch = previous_run.batch
    if batch and (
        batch.case_id != case.pk
        or (
            batch.requested_by_id != actor.pk
            and not (previous_run and previous_run.batch_id == batch.pk)
        )
    ):
        raise ValidationError("Choose an analysis batch requested by you in this case.")
    specification = get_operation(operation)
    parameters = {} if parameters is None else parameters
    try:
        valid_parameters = isinstance(parameters, dict) and len(
            json.dumps(parameters, allow_nan=False).encode()
        ) <= (65536 if operation == "question_synthesis" else 32768)
    except ValueError, TypeError:
        valid_parameters = False
    if not valid_parameters:
        raise ValidationError("Analysis settings exceed the supported limit.")
    # Execution settings are operator-owned, never accepted from a form or rerun.
    parameters = {
        key: value for key, value in parameters.items() if key != "_execution"
    }
    execution = snapshot_execution(operation, parameters)
    external = bool(execution and execution["location"] == "external")
    if external and approved_execution is not None and approved_execution != execution:
        raise AnalysisError("execution_changed")
    if external and not external_processing:
        if previous_run:
            raise AnalysisError("retry_approval_required")
        raise ValidationError("Approve external processing for this operation.")
    external_processing = external
    snapshots = snapshot_sources(case, selections, allowed_kinds=specification.kinds)
    submission_id = UUID(_uuid(submission_id or uuid4()))
    existing = AnalysisRun.objects.filter(submission_id=submission_id).first()
    if existing:

        def comparable(values):
            return [{k: v for k, v in x.items() if k != "id"} for x in values]

        if (
            existing.case_id != case.pk
            or existing.requested_by_id != actor.pk
            or existing.operation != operation
            or {
                key: value
                for key, value in existing.parameters.items()
                if key != "_execution"
            }
            != parameters
            or comparable(existing.inputs) != comparable(snapshots)
            or existing.external_processing != external_processing
            or existing.previous_run_id != (previous_run.pk if previous_run else None)
            or existing.batch_id != (batch.pk if batch else None)
        ):
            raise ValidationError(
                "This submission was already used for different analysis settings."
            )
        return existing, False
    if previous_run:
        if previous_run.case_id != case.pk:
            raise Http404("Processing not found.")
        if (external or retry_confirmation) and not constant_time_compare(
            retry_confirmation,
            retry_execution_confirmation(
                actor.pk, previous_run.pk, submission_id, execution
            ),
        ):
            raise AnalysisError("retry_approval_required")
    if (
        (require_configured_inference or (operation == "transcription" and external))
        and requires_inference(operation, parameters)
        and not inference_configured(execution, operation)
    ):
        if not external:
            raise AnalysisError("local_ai_not_configured")
        if operation == "transcription":
            raise AnalysisError("gateway_transcription_not_configured")
        raise AnalysisError("gateway_not_configured")
    if execution is not None:
        parameters = parameters | {"_execution": execution}
    if len(json.dumps(parameters, allow_nan=False).encode()) > (
        65536 if operation == "question_synthesis" else 32768
    ):
        raise ValidationError("Analysis settings exceed the supported limit.")
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
        batch=batch,
    )
    resolved = resolve_inputs(run, case)
    if operation == "question_synthesis":
        from text.analysis.synthesis import records_for_selection

        records_for_selection(resolved, run.parameters)
    try:
        with transaction.atomic():
            run.save()
    except IntegrityError:
        # Distinct case locks do not serialize a shared submission UUID.
        if AnalysisRun.objects.filter(submission_id=submission_id).exists():
            raise ValidationError(
                "This submission was already used for different analysis settings."
            ) from None
        raise
    from core.analysis.tasks import process_analysis

    process_analysis.enqueue(str(run.pk))
    return run, True


def validate_output(data, sources):
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("summary"), str)
        or not isinstance(data.get("metrics", {}), dict)
    ):
        raise AnalysisError("invalid_result")
    if data.get("outcome", "complete") not in (
        "complete",
        "partial",
        "empty",
        "insufficient",
        "unsupported",
    ):
        raise AnalysisError("invalid_result")
    try:
        size = len(json.dumps(data, allow_nan=False).encode())
    except ValueError, TypeError, RecursionError:
        raise AnalysisError("invalid_result") from None
    if size > 4 * 1024 * 1024:
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
            or not isinstance(item.get("values", {}), dict)
        ):
            raise AnalysisError("invalid_result")
        refs = item.get("source_ids", [])
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or ref not in source_map for ref in refs)
        ):
            raise AnalysisError("invalid_anchor")
        anchors = item.get("anchors", [])
        if not isinstance(anchors, list):
            raise AnalysisError("invalid_anchor")
        for anchor in anchors:
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


def _extracted_passages(result, sources):
    sources_by_id = {str(source.id): source for source in sources}
    for item_index, item in enumerate(result.data.get("items", [])):
        if not item.get("text"):
            continue
        for ref in item["source_ids"]:
            source = sources_by_id[ref]
            yield ExtractedPassage(
                case_id=result.case_id,
                result=result,
                kind=source.kind,
                evidence_id=source.item.pk,
                evidence_sha256=source.item.sha256,
                attachment_id=source.attachment.pk if source.attachment else None,
                text=item["text"],
                item_index=item_index,
                anchors=item.get("anchors", []),
            )


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
    publication_finished = False
    stage = "authorization"
    try:
        case = _authorize_run(run)
        sources = resolve_inputs(run, case)
        try:
            operation = get_operation(run.operation)
        except ValidationError as error:
            raise AnalysisError("processing_version_unavailable") from error
        if operation.version != run.method_version:
            raise AnalysisError("processing_version_unavailable")
        from core.analysis.gateway import GatewayClient

        execution = validate_execution(run)

        def authorize(**kw):
            return authorize_run(run, **kw)

        purpose = requires_inference(run.operation, run.parameters)
        client = GatewayClient(run, authorize)
        if purpose and execution is not None and execution["backend"] == "local":
            from core.analysis.local import LocalClient

            client = LocalClient(authorize, execution)
        kwargs = {}
        if run.operation == "transcription":
            kwargs["client"] = client
        if run.operation in {
            "ocr",
            "plate_ocr",
            "geocode_locations",
            "geographic_context",
        }:
            kwargs["authorize"] = authorize
        if purpose == "text" and (
            run.external_processing
            or (execution is not None and execution["backend"] == "local")
        ):
            kwargs["infer"] = client.infer
        parameters = dict(run.parameters)
        if run.operation in {"geocode_locations", "geographic_context"}:
            parameters["external_processing"] = run.external_processing
        stage = "analysis"
        data = operation.function(sources, parameters, **kwargs)
        stage = "validation"
        if not isinstance(data, dict):
            raise AnalysisError("invalid_result")
        if "_import_analysis" in parameters:
            from phone.analysis.ingest import describe_import_selection

            describe_import_selection(data, run)
        if "_reprocessing" in parameters:
            from core.analysis.reprocessing import describe_reprocessing_selection

            describe_reprocessing_selection(data, run)
        artifacts = data.pop("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 500:
            raise AnalysisError("invalid_preview")
        data = validate_output(data, sources)
        stage = "publication"
        with transaction.atomic():
            case = authorize_run(run, lock=True)
            current = AnalysisRun.objects.select_for_update().get(pk=run.pk)
            if current.status != "running":
                return current.status
            if (
                run.operation == "imessage_import"
                and run.parameters.get("phase") == "import"
            ):
                from core.case_search.index_hooks import collect_import_messages
                from phone.analysis.deterministic.imessage import apply_import

                with collect_import_messages(case.pk):
                    apply_import(run, data, sources)
                # Publication adds exact message membership; it shares the result bound.
                data = validate_output(data, sources)
            result = AnalysisResult.objects.create(
                case=case,
                run=run,
                data=data,
                provenance={
                    "method_version": run.method_version,
                    "operation": run.operation,
                    "external_processing": run.external_processing,
                    "source_inputs": run.inputs,
                    **({"execution": execution} if execution is not None else {}),
                    **(
                        {"local_usage": client.usage}
                        if execution is not None
                        and execution["backend"] == "local"
                        and purpose
                        else {}
                    ),
                },
            )
            from core.analysis.catalogue import project_result

            project_result(result)
            totals = {kind: 0 for kind in AnalysisArtifact.Kind.values}
            reference_keys = set()
            prepared_findings = set()
            source_ids = {str(s.id) for s in sources}
            for payload in artifacts:
                raw, values = decode_artifact(payload, source_ids)
                kind = values["kind"]
                finding_index = values.get("metadata", {}).get("finding_index")
                if finding_index is not None:
                    if (
                        finding_index >= len(data.get("items", []))
                        or values["source_id"]
                        not in data["items"][finding_index]["source_ids"]
                        or finding_index in prepared_findings
                    ):
                        raise AnalysisError("invalid_artifact")
                    prepared_findings.add(finding_index)
                totals[kind] += len(raw)
                reference = kind == AnalysisArtifact.Kind.GEOGRAPHIC_REFERENCE
                limit = (
                    MAX_REFERENCE_TOTAL_BYTES if reference else MAX_PREVIEW_TOTAL_BYTES
                )
                if totals[kind] > limit:
                    raise AnalysisError("invalid_artifact")
                if reference:
                    key = values["metadata"]["reference_key"]
                    if key in reference_keys:
                        raise AnalysisError("invalid_artifact")
                    reference_keys.add(key)
                artifact = AnalysisArtifact(
                    case=case,
                    result=result,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    **values,
                )
                filename = "reference.json" if reference else "preview.jpg"
                artifact.file.save(filename, ContentFile(raw), save=False)
                stored_files.append((artifact.file.storage, artifact.file.name))
                artifact.save()
            if run.operation in {"native_text", "ocr", "plate_ocr", "transcription"}:
                for batch in batched(
                    _extracted_passages(result, sources), 500, strict=False
                ):
                    ExtractedPassage.objects.bulk_create(batch)
            current.status = data.get("outcome", "complete")
            current.progress = 100
            current.finished_at = timezone.now()
            current.save(update_fields=["status", "progress", "finished_at"])
            from core.case_search.indexing import result_published

            result_published(result)
            if run.parameters.get("automatic_followup"):
                from core.analysis.automatic import follow_up_extraction

                follow_up_extraction(run, result)
            if (
                run.operation == "imessage_import"
                and run.parameters.get("phase") == "import"
            ):
                from phone.analysis.ingest import start_import_analysis

                start_import_analysis(run, data)
            publication_finished = True
            return str(result.pk)
    except (PermissionDenied, Http404) as error:
        code = "access_removed"
        log_analysis_failure(run, error, code, stage)
    except (AnalysisError, GeocodingError) as error:
        code = error.code
        log_analysis_failure(run, error, code, stage)
    except Exception as error:
        # Parser/provider strings can contain private source contents.
        code = "processing_failed"
        log_analysis_failure(run, error, code, stage)
    from core.storage import delete_original

    # A failed COMMIT acknowledgement or on_commit callback can follow a durable
    # publication. Only a failure inside its body proves these files are unused.
    if not publication_finished:
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


def address_estimate(finding):
    """An address lookup can be selected only when it returned usable coordinates."""
    values = finding.get("values", {})
    try:
        return coordinate(values.get("latitude"), values.get("longitude"))
    except ValueError:
        return None


@transaction.atomic
def set_address_estimate(actor, case_id, run_id, item_index, *, use):
    case = get_case(actor, case_id, edit=True, lock=True)
    result = (
        AnalysisResult.objects.select_related("run")
        .filter(
            case=case,
            run_id=run_id,
            run__case=case,
            run__operation="geocode_locations",
            run__status__in=("complete", "partial"),
        )
        .filter(Q(run__previous_run__isnull=True) | Q(run__previous_run__case=case))
        .first()
    )
    if result is None:
        raise Http404("Address estimate not found.")
    items = result.data.get("items", [])
    if not 0 <= item_index < len(items) or address_estimate(items[item_index]) is None:
        raise Http404("Address estimate not found.")
    resolve_inputs(result.run, case, require_current_review=False)
    latest = result.reviews.filter(case=case, item_index=item_index).first()
    in_use = latest is not None and latest.decision == AnalysisReview.Decision.ACCEPTED
    if use != in_use:
        AnalysisReview.objects.create(
            case=case,
            result=result,
            reviewed_by=actor,
            decision=AnalysisReview.Decision.ACCEPTED
            if use
            else AnalysisReview.Decision.UNRESOLVED,
            item_index=item_index,
        )

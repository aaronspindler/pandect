"""Reopen exact retained analysis sources and validate their dependencies."""

import hashlib
from copy import copy
from itertools import batched

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import Http404

from core.analysis.dependencies import validate_references
from core.analysis.errors import AnalysisError
from core.analysis.operations import PAIR_MESSAGE_OPERATIONS
from core.analysis.source_limits import check_operation_source_limits
from core.analysis.sources import Source
from core.evidence.queries import scoped_evidence, text_sha256
from core.evidence.types import CALL_KINDS, KINDS
from core.models import AnalysisResult
from core.storage import private_file
from phone.models import CallEvidenceAttachment


class SourceResolutionCache:
    """Request-local source lookups; display readers need no complete text bodies."""

    def __init__(self, case, *, with_content=True, active_only=False):
        self.case = case
        self.with_content = with_content
        self.active_only = active_only
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
                        query = query.annotate(content_sha256=text_sha256("body"))
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
                query = query.defer("transcript").annotate(
                    has_transcript=~Q(transcript="")
                )
            self.attachments.update(dict.fromkeys(chunk))
            self.attachments.update({str(item.pk): item for item in query})

    def original(self, selected):
        item = self.originals.get((selected["kind"], selected["evidence_id"]))
        if item is None:
            raise Http404("Source not found.")
        if self.active_only and item.is_archived:
            raise AnalysisError("source_archived")
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
    run,
    case,
    *,
    require_current_review=True,
    cache=None,
    finding_depth=0,
    material_checked=False,
):
    if (
        run.operation == "text_topics"
        and run.parameters.get("_search_generation") == "briefs"
        and (cache is None or cache.with_content)
    ):
        from core.case_search.generation_briefs import resolve_brief

        return resolve_brief(run, case)
    cache = cache or SourceResolutionCache(case)
    if run.inputs and cache.with_content and not material_checked and not finding_depth:
        try:
            parameters = run.parameters if run.operation == "question_synthesis" else {}
            check_operation_source_limits(case, run.operation, parameters, run.inputs)
        except ValidationError as error:
            if run._state.adding:
                raise
            raise AnalysisError(
                "source_material_limit"
                if getattr(error, "code", "") == "source_material_limit"
                else "source_unavailable"
            ) from error
    sources = resolve_snapshots(
        run.inputs,
        case,
        run_id=run.pk,
        require_current_review=require_current_review,
        cache=cache,
    )
    if run.operation in PAIR_MESSAGE_OPERATIONS and any(
        source.kind == "text-message" and source.item.recipient_id is None
        for source in sources
    ):
        raise AnalysisError("direct_messages_required")
    if (
        run.operation == "session_summaries"
        and "_saved_session_range" in run.parameters
    ):
        from phone.saved_summary_ranges import checked_session_range

        checked_session_range(case, run, sources)
    if run.operation == "claim_comparison" and any(
        source.item.is_archived for source in sources
    ):
        raise AnalysisError("source_archived")
    if run.operation in {"question_synthesis", "sighting_group"}:
        validate_references(run, sources, case, cache=cache, depth=finding_depth)
    elif run.operation == "journey_claims":
        from geo.journey_claim_checks import check_comparison_inputs

        check_comparison_inputs(run, sources, case, cache, finding_depth)
    return sources


def resolve_active_inputs(
    run, case, *, require_current_review=True, material_checked=False
):
    return resolve_inputs(
        run,
        case,
        require_current_review=require_current_review,
        cache=SourceResolutionCache(case, active_only=True),
        material_checked=material_checked,
    )


def resolve_processing_inputs(run, case, *, material_checked=False):
    sighting = run.operation == "sighting_group"
    sources = resolve_active_inputs(
        run,
        case,
        require_current_review=not sighting,
        material_checked=material_checked,
    )
    if sighting:
        from core.analysis.sightings import validate_current_sighting

        validate_current_sighting(run, case, sources)
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
            source_identifier = selected["id"]
            stored_checksum = selected["sha256"]
        except Http404, ValueError, KeyError, ValidationError:
            raise AnalysisError("source_unavailable") from None
        checksum = attachment.sha256 if attachment else item.sha256
        if checksum != stored_checksum:
            raise AnalysisError("source_changed")
        if "call_person_ids" in selected:
            participants = selected["call_person_ids"]
            if (
                selected["kind"] not in CALL_KINDS
                or not item.import_row_id
                or not isinstance(participants, list)
                or len(participants) != 2
            ):
                raise AnalysisError("source_changed")
            current = [item.sender_id, item.recipient_id]
            if any(
                pinned is not None and pinned != str(person)
                for pinned, person in zip(participants, current, strict=True)
            ):
                raise AnalysisError("source_changed")
            # A later link must not change a previously selected source's people.
            if any(
                pinned is None and person is not None
                for pinned, person in zip(participants, current, strict=True)
            ):
                item = copy(item)
                if participants[0] is None:
                    item.sender_id = None
                if participants[1] is None:
                    item.recipient_id = None
        identity = (
            selected["kind"],
            selected["evidence_id"],
            selected.get("attachment_id"),
        )
        if identity not in validated_originals:
            record = attachment or item
            if getattr(record, "file", None):
                try:
                    original_file = private_file(record, case.pk)
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
            if cache.with_content and (
                attachment or selected["kind"] == "text-message"
            ):
                # Quoting a passage requires the same captured message or transcript.
                # original_bytes() serializes the retained record, including its file
                # manifest when present; it does not read a recording.
                if (
                    hashlib.sha256((attachment or item).original_bytes()).hexdigest()
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
                source_identifier,
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

"""Server-rendered catalogue detail and operator processing decisions."""

import json
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from pandect.catalog.models import CatalogItem
from pandect.identities.models import (
    IdentityAssertion,
    IdentityCandidate,
    IdentityCandidateDecision,
    IdentityObservation,
    Subject,
    SubjectReference,
    SubjectReferenceDecision,
)
from pandect.identities.reference_history import record_reference_decision
from pandect.identities.services import (
    StaleIdentityCandidateError,
    current_identity_candidates,
    current_reference_vectors,
    current_source_observations,
    decide_identity_candidate,
    observation_is_gallery_eligible,
    observation_source_is_current,
)
from pandect.processing.forms import (
    CandidateDecisionForm,
    RemoteConsentForm,
    SubjectForm,
    SubjectReferenceForm,
    TimestampPromotionForm,
)
from pandect.processing.identity_rematch import create_gallery_rematch_job
from pandect.processing.models import (
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
    TimestampClaim,
    TimestampDecision,
)
from pandect.processing.remote.followup import (
    FOLLOWUP_NOTICE_VERSION,
    FOLLOWUP_WORKFLOW,
    build_followup_plan,
    create_consented_followup_run,
)
from pandect.processing.remote.workflows import (
    REMOTE_NOTICE_VERSION,
    REMOTE_PROCESSOR_VERSION,
    RemotePlanError,
    RemoteWorkflowError,
    build_remote_plan,
    create_consented_remote_run,
)
from pandect.processing.services import (
    LOCAL_STAGE_DEFINITIONS,
    PROCESSOR_VERSION,
    ProcessingError,
    create_local_run,
    source_fingerprint,
)

NOTICE_MESSAGES = {
    "rerun_started": "Local processing started. Earlier results remain in the history.",
    "retry_started": "Retrying local processing. Earlier results are preserved.",
    "timestamp_promoted": (
        "The occurrence time was updated. Your timestamp decision has been recorded."
    ),
    "subject_created": "The person or subject was added to your local gallery.",
    "reference_added": "The observation was added to the subject reference gallery.",
    "reference_updated": "The subject reference was updated and rematching started.",
    "reference_removed": "The subject reference was removed and rematching started.",
    "candidate_confirmed": "The identity match was saved as your confirmed decision.",
    "candidate_rejected": "The suggested identity match was rejected.",
    "remote_started": "The consented remote processing run was created.",
    "remote_followup_started": (
        "The separately consented transcript/video follow-up run was created."
    ),
}


def _item(item_id) -> CatalogItem:
    return get_object_or_404(
        CatalogItem.objects.select_related(
            "image",
            "video",
            "audio",
            "event",
            "note",
            "message__sender",
            "message__reply_to",
            "message__image_message__image__catalog_item",
            "message__video_message__video__catalog_item",
        ).prefetch_related("message__recipients__participant"),
        pk=item_id,
    )


def _optional_relation(instance, name):
    try:
        return getattr(instance, name)
    except ObjectDoesNotExist:
        return None


def _schedule_identity_rematch(decision: SubjectReferenceDecision) -> None:
    """Persist a bounded global fan-out cursor in the decision transaction."""
    create_gallery_rematch_job(decision)


def _canonical_facts(item: CatalogItem) -> list[dict[str, object]]:
    facts: list[dict[str, object]] = [
        {"label": "Item type", "value": item.get_item_type_display()},
        {"label": "Title", "value": item.title or "Untitled"},
        {
            "label": "When it happened",
            "value": item.occurred_at or "Unknown",
            "is_timestamp": item.occurred_at is not None,
        },
        {
            "label": "Added to catalogue",
            "value": item.catalogued_at,
            "is_timestamp": True,
        },
        {"label": "Item ID", "value": item.pk, "monospace": True},
    ]
    if item.item_type == CatalogItem.ItemType.IMAGE:
        image = _optional_relation(item, "image")
        if image:
            facts.extend(
                (
                    {
                        "label": "Original filename",
                        "value": image.original_filename or "Unknown",
                    },
                    {
                        "label": "Recorded file format (MIME type)",
                        "value": image.mime_type or "Unknown",
                    },
                    {
                        "label": "Recorded dimensions",
                        "value": (
                            f"{image.width} × {image.height}"
                            if image.width is not None and image.height is not None
                            else "Unknown"
                        ),
                    },
                )
            )
    elif item.item_type == CatalogItem.ItemType.VIDEO:
        video = _optional_relation(item, "video")
        if video:
            facts.extend(
                (
                    {
                        "label": "Original filename",
                        "value": video.original_filename or "Unknown",
                    },
                    {
                        "label": "Recorded file format (MIME type)",
                        "value": video.mime_type or "Unknown",
                    },
                    {
                        "label": "Recorded duration",
                        "value": (
                            video.duration if video.duration is not None else "Unknown"
                        ),
                    },
                    {
                        "label": "Recorded dimensions",
                        "value": (
                            f"{video.width} × {video.height}"
                            if video.width is not None and video.height is not None
                            else "Unknown"
                        ),
                    },
                )
            )
    elif item.item_type == CatalogItem.ItemType.AUDIO:
        audio = _optional_relation(item, "audio")
        if audio:
            facts.extend(
                (
                    {
                        "label": "Original filename",
                        "value": audio.original_filename or "Unknown",
                    },
                    {
                        "label": "Recorded file format (MIME type)",
                        "value": audio.mime_type or "Unknown",
                    },
                    {
                        "label": "Recorded duration",
                        "value": (
                            audio.duration if audio.duration is not None else "Unknown"
                        ),
                    },
                )
            )
    elif item.item_type == CatalogItem.ItemType.EVENT:
        event = _optional_relation(item, "event")
        if event:
            facts.extend(
                (
                    {
                        "label": "Event ends",
                        "value": event.ended_at or "Unknown",
                        "is_timestamp": event.ended_at is not None,
                    },
                    {
                        "label": "Description",
                        "value": event.description or "Not recorded",
                        "long": True,
                    },
                )
            )
    elif item.item_type == CatalogItem.ItemType.NOTE:
        note = _optional_relation(item, "note")
        if note:
            facts.append({"label": "Note text", "value": note.body, "long": True})
    else:
        message = _optional_relation(item, "message")
        if message:
            recipients = ", ".join(
                f"{recipient.participant} ({recipient.get_role_display()})"
                for recipient in message.recipients.all()
            )
            facts.extend(
                (
                    {
                        "label": "Message body",
                        "value": message.body or "Blank",
                        "long": True,
                    },
                    {"label": "Sender", "value": message.sender or "Unknown"},
                    {"label": "Recipients", "value": recipients or "None recorded"},
                    {"label": "Reply to", "value": message.reply_to_id or "None"},
                )
            )
            image_message = _optional_relation(message, "image_message")
            video_message = _optional_relation(message, "video_message")
            if image_message:
                facts.append(
                    {
                        "label": "Referenced image",
                        "value": image_message.image.catalog_item,
                        "linked_item_id": image_message.image.catalog_item_id,
                    }
                )
            if video_message:
                facts.append(
                    {
                        "label": "Referenced video",
                        "value": video_message.video.catalog_item,
                        "linked_item_id": video_message.video.catalog_item_id,
                    }
                )
    return facts


def _selected_run(
    item: CatalogItem, raw_run_id: str = ""
) -> tuple[
    ProcessingRun | None,
    bool,
    object | None,
    tuple[ProcessingRun, ...],
]:
    """Select a full local base and compatible supplemental evidence runs.

    Remote consent, follow-up, and identity-rematch runs are intentionally
    partial workflows. They may add evidence to the default view, but must not
    replace the newest full local run that owns the deterministic extraction.
    An explicit history selection remains isolated to exactly that run.
    """
    runs = ProcessingRun.objects.filter(catalog_item=item)
    current_source = None
    try:
        current_source = source_fingerprint(item.pk)
    except ProcessingError:
        current_runs: list[ProcessingRun] = []
    else:
        current_runs = list(
            runs.filter(
                source_fingerprint=current_source.sha256,
                source_size=current_source.size_bytes,
            )
            .exclude(status=ProcessingRun.Status.SUPERSEDED)
            .order_by("-created_at", "id")
        )

    def configuration(run: ProcessingRun) -> dict[str, object]:
        value = run.configuration
        return value if isinstance(value, dict) else {}

    def workflow(run: ProcessingRun) -> str:
        value = configuration(run).get("workflow", "")
        return value if isinstance(value, str) else ""

    current_run = next(
        (
            run
            for run in current_runs
            if run.scope == ProcessingRun.Scope.LOCAL
            and run.processor_version == PROCESSOR_VERSION
            and workflow(run) != "identity_gallery_rematch"
        ),
        None,
    )

    supplemental: list[ProcessingRun] = []
    if current_run is not None:
        remote_run = next(
            (
                run
                for run in current_runs
                if run.scope == ProcessingRun.Scope.REMOTE
                and run.processor_version == REMOTE_PROCESSOR_VERSION
                and workflow(run) != FOLLOWUP_WORKFLOW
            ),
            None,
        )
        if remote_run is not None:
            supplemental.append(remote_run)
            followup_run = next(
                (
                    run
                    for run in current_runs
                    if run.scope == ProcessingRun.Scope.REMOTE
                    and run.processor_version == REMOTE_PROCESSOR_VERSION
                    and workflow(run) == FOLLOWUP_WORKFLOW
                    and str(configuration(run).get("aggregate_run_id", ""))
                    == str(remote_run.pk)
                ),
                None,
            )
            if followup_run is not None:
                supplemental.append(followup_run)

        rematch_modalities: set[str] = set()
        for run in current_runs:
            run_configuration = configuration(run)
            if (
                run.scope != ProcessingRun.Scope.LOCAL
                or run.processor_version != PROCESSOR_VERSION
                or workflow(run) != "identity_gallery_rematch"
            ):
                continue
            modality = run_configuration.get("modality")
            if not isinstance(modality, str) or modality in rematch_modalities:
                continue
            rematch_modalities.add(modality)
            supplemental.append(run)

    default_evidence_runs = (current_run, *supplemental) if current_run else ()
    if raw_run_id:
        try:
            run_id = UUID(raw_run_id)
        except (TypeError, ValueError):
            return None, False, current_source, ()
        selected = runs.filter(pk=run_id).first()
        historical = bool(
            selected is not None
            and selected.pk not in {run.pk for run in default_evidence_runs}
        )
        return (
            selected,
            historical,
            current_source,
            (selected,) if selected is not None else (),
        )
    return current_run, False, current_source, default_evidence_runs


def _result_record(result: ProcessingResult) -> dict[str, object]:
    return {
        "id": result.pk,
        "kind": result.get_result_kind_display(),
        "stage": result.stage.get_kind_display(),
        "unit_key": result.stage.unit_key,
        "text": result.text,
        "data_json": (
            json.dumps(
                result.data, indent=2, ensure_ascii=False, sort_keys=True, default=str
            )
            if result.data
            else ""
        ),
        "confidence": result.confidence,
        "uncertainty": result.uncertainty,
        "processor": result.processor_name,
        "processor_version": result.processor_version,
        "schema_version": result.schema_version,
        "created_at": result.created_at,
    }


def _evidence_tiers(
    runs: tuple[ProcessingRun, ...],
) -> dict[str, list[dict[str, object]]]:
    tiers = {value: [] for value, _label in ProcessingResult.EvidenceTier.choices}
    if not runs:
        return tiers
    for result in (
        ProcessingResult.objects.filter(stage__run__in=runs)
        .select_related("stage")
        .order_by("created_at", "pk")
    ):
        tiers[result.evidence_tier].append(_result_record(result))
    return tiers


def _run_history(item: CatalogItem) -> list[ProcessingRun]:
    return list(
        ProcessingRun.objects.filter(catalog_item=item).annotate(
            stage_count=Count("stages"),
            failed_stage_count=Count(
                "stages",
                filter=Q(stages__status=ProcessingStage.Status.FAILED),
            ),
        )
    )


def _timestamp_claims(
    item: CatalogItem,
    run: ProcessingRun | None,
    *,
    bound_form: TimestampPromotionForm | None = None,
    bound_claim_id=None,
) -> list[dict[str, object]]:
    if run is None:
        return []
    claims = TimestampClaim.objects.filter(
        catalog_item=item,
        stage__run=run,
    ).select_related("stage")
    records = []
    for claim in claims:
        form = (
            bound_form
            if bound_form is not None and claim.pk == bound_claim_id
            else TimestampPromotionForm(
                claim=claim,
                auto_id=f"id_{claim.pk}_%s",
            )
        )
        records.append({"claim": claim, "form": form})
    return records


def _identity_records(
    item: CatalogItem, *, canonical_source=None
) -> list[dict[str, object]]:
    candidates = (
        IdentityCandidate.objects.select_related(
            "subject",
            "producing_run",
            "producing_stage",
        )
        .prefetch_related(
            Prefetch(
                "decision_history",
                queryset=IdentityCandidateDecision.objects.order_by(
                    "decided_at", "created_at", "id"
                ),
            )
        )
        .order_by("candidate_set_id", "rank", "id")
    )
    observations = list(
        IdentityObservation.objects.filter(catalog_item=item)
        .defer("embedding")
        .prefetch_related(
            Prefetch("identity_candidates", queryset=candidates),
            "subject_reference__subject",
            "identity_assertions__subject",
        )
    )
    observations = current_source_observations(
        observations,
        canonical_sources=(
            {item.pk: canonical_source} if canonical_source is not None else None
        ),
    )
    references = list(
        SubjectReference.objects.select_related(
            "observation",
            "observation__catalog_item",
        ).all()
    )
    reference_vectors = (
        current_reference_vectors(
            references,
            canonical_sources={item.pk: canonical_source},
        )
        if canonical_source is not None
        else ()
    )
    records = []
    for observation in observations:
        if not observation_is_gallery_eligible(observation):
            continue
        reference = _optional_relation(observation, "subject_reference")
        active_assertion = next(
            (
                assertion
                for assertion in observation.identity_assertions.all()
                if assertion.status == IdentityAssertion.Status.ACTIVE
            ),
            None,
        )
        historical_candidates = list(observation.identity_candidates.all())
        current_candidates = (
            current_identity_candidates(
                observation,
                candidates=historical_candidates,
                reference_vectors=reference_vectors,
                canonical_source=canonical_source,
            )
            if canonical_source is not None
            else ()
        )
        candidate_records = []
        for candidate in current_candidates:
            history = list(candidate.decision_history.all())
            candidate_records.append(
                {
                    "candidate": candidate,
                    "decision_label": candidate.get_decision_display(),
                    "decision_history": history,
                    "producing_run_id": candidate.producing_run_id,
                    "producing_stage_id": candidate.producing_stage_id,
                    "actionable": (
                        candidate.decision == IdentityCandidate.Decision.SUGGESTED
                    ),
                }
            )
        records.append(
            {
                "id": observation.pk,
                "modality": observation.get_modality_display(),
                "unit_key": observation.unit_key,
                "source_location_json": json.dumps(
                    observation.source_location,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                "quality": observation.get_quality_status_display(),
                "quality_status": observation.quality_status,
                "quality_score": observation.quality_score,
                "feature_space": observation.feature_space,
                "reference": reference,
                "active_assertion": active_assertion,
                "candidates": candidate_records,
                "has_stale_candidates": bool(historical_candidates)
                and not candidate_records,
            }
        )
    return records


def _reference_decision_history(item: CatalogItem) -> list[SubjectReferenceDecision]:
    """Return immutable reference decisions without loading opaque features."""
    return list(
        SubjectReferenceDecision.objects.filter(
            observation__catalog_item=item,
        )
        .select_related("observation", "subject", "previous_subject")
        .order_by("decided_at", "created_at", "id")
    )


def _detail_context(
    request: HttpRequest,
    item: CatalogItem,
    *,
    subject_form: SubjectForm | None = None,
    reference_form: SubjectReferenceForm | None = None,
    reference_observation_id=None,
    timestamp_form: TimestampPromotionForm | None = None,
    timestamp_claim_id=None,
    action_error: str = "",
) -> dict[str, object]:
    (
        selected_run,
        selected_run_is_historical,
        canonical_source,
        evidence_runs,
    ) = _selected_run(item, request.GET.get("run", ""))
    tiers = _evidence_tiers(evidence_runs)
    run_history = _run_history(item)
    artifacts = []
    if selected_run is not None:
        artifacts = list(
            DerivedArtifact.objects.filter(stage__run=selected_run).select_related(
                "stage"
            )
        )
    status_stages = list(selected_run.stages.all()) if selected_run is not None else []
    status_active = bool(
        selected_run
        and not selected_run_is_historical
        and selected_run.status
        in (ProcessingRun.Status.PENDING, ProcessingRun.Status.RUNNING)
    )
    remote_plan = None
    remote_plan_error = ""
    remote_followup_plan = None
    remote_followup_plan_error = ""
    if settings.CLOUDFLARE_AI_ENABLED:
        try:
            remote_plan = build_remote_plan(item)
        except RemotePlanError as error:
            remote_plan_error = error.public_message
        try:
            remote_followup_plan = build_followup_plan(item)
        except RemotePlanError as error:
            remote_followup_plan_error = error.public_message
    return {
        "item": item,
        "canonical_facts": _canonical_facts(item),
        "selected_run": selected_run,
        "selected_run_is_historical": selected_run_is_historical,
        "status_stages": status_stages,
        "status_active": status_active,
        "run_history": run_history,
        "deterministic_results": tiers[ProcessingResult.EvidenceTier.DETERMINISTIC],
        "model_results": tiers[ProcessingResult.EvidenceTier.MODEL_GENERATED],
        "canonical_results": tiers[ProcessingResult.EvidenceTier.CANONICAL],
        "operator_results": tiers[ProcessingResult.EvidenceTier.OPERATOR_CONFIRMED],
        "timestamp_claims": _timestamp_claims(
            item,
            selected_run,
            bound_form=timestamp_form,
            bound_claim_id=timestamp_claim_id,
        ),
        "timestamp_decisions": item.timestamp_decisions.select_related("claim").all(),
        "identity_records": _identity_records(
            item,
            canonical_source=canonical_source,
        ),
        "reference_decision_history": _reference_decision_history(item),
        "subjects": Subject.objects.order_by("label", "id"),
        "subject_form": subject_form or SubjectForm(),
        "reference_form": reference_form,
        "reference_observation_id": reference_observation_id,
        "artifacts": artifacts,
        "can_process": item.item_type in LOCAL_STAGE_DEFINITIONS,
        "notice": NOTICE_MESSAGES.get(request.GET.get("notice", ""), ""),
        "action_error": action_error,
        "remote_enabled": settings.CLOUDFLARE_AI_ENABLED,
        "remote_plan": remote_plan,
        "remote_plan_error": remote_plan_error,
        "remote_consent_form": (
            RemoteConsentForm(
                auto_id="id_remote_%s",
                initial={
                    "manifest_hash": remote_plan.manifest.sha256,
                    "notice_version": REMOTE_NOTICE_VERSION,
                },
            )
            if remote_plan is not None
            else None
        ),
        "remote_followup_plan": remote_followup_plan,
        "remote_followup_plan_error": remote_followup_plan_error,
        "remote_followup_consent_form": (
            RemoteConsentForm(
                auto_id="id_followup_%s",
                initial={
                    "manifest_hash": remote_followup_plan.manifest.sha256,
                    "notice_version": FOLLOWUP_NOTICE_VERSION,
                },
            )
            if remote_followup_plan is not None
            else None
        ),
    }


@require_GET
def item_detail(request: HttpRequest, item_id) -> HttpResponse:
    """Show canonical facts, durable progress, tiered evidence, and decisions."""
    item = _item(item_id)
    return render(
        request, "processing/item_detail.html", _detail_context(request, item)
    )


def _status_context(
    item: CatalogItem,
    run: ProcessingRun | None,
    *,
    historical: bool = False,
) -> dict[str, object]:
    stages = list(run.stages.all()) if run is not None else []
    active = bool(
        run
        and not historical
        and run.status
        in (
            ProcessingRun.Status.PENDING,
            ProcessingRun.Status.RUNNING,
        )
    )
    return {
        "item": item,
        "selected_run": run,
        "selected_run_is_historical": historical,
        "stages": stages,
        "active": active,
        "can_process": item.item_type in LOCAL_STAGE_DEFINITIONS,
    }


@require_GET
def processing_status(request: HttpRequest, item_id) -> HttpResponse:
    """Return a bounded HTMX fragment backed only by durable Django state."""
    item = get_object_or_404(CatalogItem, pk=item_id)
    run, historical, _canonical_source, _evidence_runs = _selected_run(
        item, request.GET.get("run", "")
    )
    return render(
        request,
        "processing/partials/status.html",
        _status_context(item, run, historical=historical),
    )


def _detail_redirect(item_id, *, run_id=None, notice="") -> HttpResponse:
    url = reverse("catalogue-item-detail", kwargs={"item_id": item_id})
    query = []
    if run_id:
        query.append(f"run={run_id}")
    if notice:
        query.append(f"notice={notice}")
    return redirect(f"{url}?{'&'.join(query)}" if query else url)


@require_POST
def rerun_local_processing(request: HttpRequest, item_id) -> HttpResponse:
    """Create a fresh local run while preserving every prior output."""
    item = get_object_or_404(CatalogItem, pk=item_id)
    try:
        run = create_local_run(item, trigger=ProcessingRun.Trigger.MANUAL)
    except ProcessingError:
        return HttpResponseBadRequest("This item cannot be processed locally.")
    return _detail_redirect(item.pk, run_id=run.pk, notice="rerun_started")


@require_POST
def retry_failed_stage(request: HttpRequest, item_id, stage_id) -> HttpResponse:
    """Translate a stage retry into a new versioned run, retaining the failure."""
    stage = get_object_or_404(
        ProcessingStage.objects.select_related("run"),
        pk=stage_id,
        run__catalog_item_id=item_id,
    )
    if stage.status != ProcessingStage.Status.FAILED:
        return HttpResponseBadRequest("Only a failed stage can be retried.")
    if stage.run.scope != ProcessingRun.Scope.LOCAL:
        return HttpResponseBadRequest(
            "Remote processing requires a newly reviewed consent manifest."
        )
    try:
        run = create_local_run(
            item_id,
            trigger=ProcessingRun.Trigger.MANUAL,
            configuration={
                "manual_retry": {
                    "stage_id": str(stage.pk),
                    "run_id": str(stage.run_id),
                    "kind": stage.kind,
                    "unit_key": stage.unit_key,
                }
            },
        )
    except ProcessingError:
        return HttpResponseBadRequest("The source is not available for a new run.")
    return _detail_redirect(item_id, run_id=run.pk, notice="retry_started")


@require_POST
def promote_timestamp(request: HttpRequest, item_id, claim_id) -> HttpResponse:
    """Promote an extracted claim only after an explicit timezone decision."""
    item = _item(item_id)
    claim = get_object_or_404(TimestampClaim, pk=claim_id, catalog_item=item)
    form = TimestampPromotionForm(request.POST, claim=claim)
    if not form.is_valid():
        context = _detail_context(
            request,
            item,
            timestamp_form=form,
            timestamp_claim_id=claim.pk,
            action_error="The timestamp decision needs attention.",
        )
        return render(request, "processing/item_detail.html", context, status=400)

    try:
        with transaction.atomic():
            locked_item = CatalogItem.objects.select_for_update().get(pk=item.pk)
            locked_claim = TimestampClaim.objects.select_related("catalog_item").get(
                pk=claim.pk,
                catalog_item=locked_item,
            )
            previous = locked_item.occurred_at
            locked_item.occurred_at = form.resulting_occurred_at
            locked_item.updated_at = timezone.now()
            locked_item.save(update_fields=("occurred_at", "updated_at"))
            decision = TimestampDecision(
                claim=locked_claim,
                catalog_item=locked_item,
                previous_occurred_at=previous,
                timezone_name=form.resolved_timezone_name,
                fold=form.resolved_fold,
                resulting_occurred_at=form.resulting_occurred_at,
                operator_note=form.cleaned_data["operator_note"],
            )
            decision.full_clean()
            decision.save()
    except ValidationError as error:
        form.add_error(None, " ".join(error.messages))
        context = _detail_context(
            request,
            item,
            timestamp_form=form,
            timestamp_claim_id=claim.pk,
            action_error="The timestamp could not be promoted.",
        )
        return render(request, "processing/item_detail.html", context, status=400)
    return _detail_redirect(item.pk, notice="timestamp_promoted")


@require_POST
def create_subject(request: HttpRequest, item_id) -> HttpResponse:
    """Create a local identity label without changing canonical item facts."""
    item = _item(item_id)
    form = SubjectForm(request.POST)
    if not form.is_valid():
        context = _detail_context(
            request,
            item,
            subject_form=form,
            action_error="The subject could not be created.",
        )
        return render(request, "processing/item_detail.html", context, status=400)
    form.save()
    return _detail_redirect(item.pk, notice="subject_created")


@require_POST
def add_subject_reference(
    request: HttpRequest, item_id, observation_id
) -> HttpResponse:
    """Add one quality-approved observation to the closed reference gallery."""
    item = _item(item_id)
    observation = get_object_or_404(
        IdentityObservation.objects.defer("embedding"),
        pk=observation_id,
        catalog_item=item,
    )
    form = SubjectReferenceForm(request.POST, auto_id="id_reference_%s")
    source_is_current = observation_source_is_current(observation)
    if form.is_valid() and not source_is_current:
        form.add_error(
            None,
            "This observation is historical because its canonical source changed.",
        )
    if form.is_valid():
        try:
            with transaction.atomic():
                reference = SubjectReference(
                    subject=form.cleaned_data["subject"],
                    observation=observation,
                    notes=form.cleaned_data["notes"],
                )
                reference.full_clean()
                reference.save()
                decision = record_reference_decision(
                    reference,
                    SubjectReferenceDecision.Action.SELECTED,
                )
                _schedule_identity_rematch(decision)
        except ValidationError as error:
            form.add_error(None, " ".join(error.messages))
        except IntegrityError:
            form.add_error(None, "This observation already has a subject reference.")
    if not form.is_valid() or form.errors:
        context = _detail_context(
            request,
            item,
            reference_form=form,
            reference_observation_id=observation.pk,
            action_error=(
                "Historical observations cannot become references after their "
                "canonical source changes."
                if not source_is_current
                else "Only an approved, unassigned observation can become a reference."
            ),
        )
        return render(request, "processing/item_detail.html", context, status=400)
    return _detail_redirect(item.pk, notice="reference_added")


@require_POST
def update_subject_reference(
    request: HttpRequest, item_id, observation_id
) -> HttpResponse:
    """Change one gallery assignment and append a bounded rematch run."""
    item = _item(item_id)
    observation = get_object_or_404(
        IdentityObservation.objects.defer("embedding"),
        pk=observation_id,
        catalog_item=item,
    )
    form = SubjectReferenceForm(request.POST, auto_id="id_reference_%s")
    if not form.is_valid():
        return HttpResponseBadRequest("The subject reference could not be updated.")
    if not observation_source_is_current(observation):
        return HttpResponseBadRequest(
            "This historical subject reference cannot be updated because its "
            "canonical source changed."
        )
    try:
        with transaction.atomic():
            reference = SubjectReference.objects.select_for_update().get(
                observation=observation
            )
            previous_subject = reference.subject
            previous_notes = reference.notes
            reference.subject = form.cleaned_data["subject"]
            reference.notes = form.cleaned_data["notes"]
            reference.full_clean()
            reference.save(update_fields=("subject", "notes"))
            decision = record_reference_decision(
                reference,
                SubjectReferenceDecision.Action.UPDATED,
                previous_subject=previous_subject,
                previous_notes=previous_notes,
            )
            _schedule_identity_rematch(decision)
    except SubjectReference.DoesNotExist:
        return HttpResponseBadRequest("The subject reference no longer exists.")
    except (ValidationError, IntegrityError):
        return HttpResponseBadRequest("The subject reference could not be updated.")
    return _detail_redirect(item.pk, notice="reference_updated")


@require_POST
def remove_subject_reference(
    request: HttpRequest, item_id, observation_id
) -> HttpResponse:
    """Remove one active gallery assignment and append a bounded rematch run."""
    item = _item(item_id)
    observation = get_object_or_404(
        IdentityObservation.objects.defer("embedding"),
        pk=observation_id,
        catalog_item=item,
    )
    try:
        with transaction.atomic():
            reference = SubjectReference.objects.select_for_update().get(
                observation=observation
            )
            decision = record_reference_decision(
                reference,
                SubjectReferenceDecision.Action.REMOVED,
            )
            reference.delete()
            _schedule_identity_rematch(decision)
    except SubjectReference.DoesNotExist:
        return HttpResponseBadRequest("The subject reference no longer exists.")
    return _detail_redirect(item.pk, notice="reference_removed")


@require_POST
def decide_candidate(request: HttpRequest, item_id, candidate_id) -> HttpResponse:
    """Confirm or reject one modality-specific candidate explicitly."""
    candidate = get_object_or_404(
        IdentityCandidate.objects.select_related("observation"),
        pk=candidate_id,
        observation__catalog_item_id=item_id,
    )
    form = CandidateDecisionForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("The candidate decision was not recognized.")
    decision = form.cleaned_data["decision"]
    try:
        decide_identity_candidate(
            candidate.pk,
            decision,
            note=form.cleaned_data["note"],
        )
    except StaleIdentityCandidateError:
        return HttpResponseBadRequest(
            "This suggestion is stale. Review the newest compatible candidate set."
        )
    notice = (
        "candidate_confirmed"
        if decision == IdentityCandidate.Decision.CONFIRMED
        else "candidate_rejected"
    )
    return _detail_redirect(item_id, notice=notice)


@require_POST
def consent_remote_processing(request: HttpRequest, item_id) -> HttpResponse:
    """Create a remote run only when the submitted current manifest is consented."""
    item = _item(item_id)
    form = RemoteConsentForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest(
            "Explicit consent to the current plan is required."
        )
    try:
        run = create_consented_remote_run(
            item,
            expected_manifest_hash=form.cleaned_data["manifest_hash"],
            notice_version=form.cleaned_data["notice_version"],
        )
    except RemoteWorkflowError as error:
        return HttpResponseBadRequest(error.public_message)
    return _detail_redirect(item.pk, run_id=run.pk, notice="remote_started")


@require_POST
def consent_remote_followup(request: HttpRequest, item_id) -> HttpResponse:
    """Create only the exact second-stage transfer the operator reviewed."""
    item = _item(item_id)
    form = RemoteConsentForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest(
            "Explicit consent to the current follow-up plan is required."
        )
    try:
        run = create_consented_followup_run(
            item,
            expected_manifest_hash=form.cleaned_data["manifest_hash"],
            notice_version=form.cleaned_data["notice_version"],
        )
    except RemoteWorkflowError as error:
        return HttpResponseBadRequest(error.public_message)
    return _detail_redirect(
        item.pk,
        run_id=run.pk,
        notice="remote_followup_started",
    )

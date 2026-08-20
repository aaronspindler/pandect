"""Durable, bounded fan-out after an operator changes the closed gallery."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.identities.models import (
    IdentityGalleryRematchJob,
    IdentityObservation,
    SubjectReferenceDecision,
)
from pandect.identities.services import (
    VIDEO_FACE_AGGREGATE_ROLE,
    current_source_observations,
    observation_is_gallery_eligible,
)
from pandect.processing.models import ProcessingRun
from pandect.processing.services import (
    IDENTITY_REMATCH_OBSERVATION_LIMIT,
    ProcessingError,
    create_identity_rematch_run,
)

logger = logging.getLogger(__name__)

GALLERY_REMATCH_BATCH_SIZE = 25
GALLERY_REMATCH_LEASE_SECONDS = 10 * 60


@dataclass(frozen=True)
class GalleryRematchReport:
    examined_items: int = 0
    runs_created: int = 0
    completed: bool = False


def _upper_item_id(modality: str):
    return (
        IdentityObservation.objects.filter(
            modality=modality,
            quality_status=IdentityObservation.QualityStatus.APPROVED,
        )
        .order_by("-catalog_item_id")
        .values_list("catalog_item_id", flat=True)
        .first()
    )


def create_gallery_rematch_job(
    decision: SubjectReferenceDecision,
) -> IdentityGalleryRematchJob:
    """Create one durable cursor in the operator-decision transaction."""
    upper_item_id = _upper_item_id(decision.observation.modality)
    completed = upper_item_id is None
    job = IdentityGalleryRematchJob.objects.create(
        decision=decision,
        modality=decision.observation.modality,
        upper_item_id=upper_item_id,
        status=(
            IdentityGalleryRematchJob.Status.SUCCEEDED
            if completed
            else IdentityGalleryRematchJob.Status.PENDING
        ),
        completed_at=timezone.now() if completed else None,
    )
    if not completed:
        enqueue_gallery_rematch_on_commit(job)
    return job


def enqueue_gallery_rematch_on_commit(
    job_or_id: IdentityGalleryRematchJob | UUID | str,
) -> None:
    """Dispatch only the durable job UUID after its transaction commits."""
    job_id = (
        job_or_id.pk if isinstance(job_or_id, IdentityGalleryRematchJob) else job_or_id
    )
    transaction.on_commit(lambda: _dispatch_gallery_rematch_safely(str(job_id)))


def _dispatch_gallery_rematch_safely(job_id: UUID | str) -> None:
    try:
        from pandect.processing.tasks import run_identity_gallery_rematch_batch

        run_identity_gallery_rematch_batch(str(job_id))
    except Exception as error:  # noqa: BLE001 - periodic reconciliation repairs it.
        logger.error(
            "Gallery-rematch dispatch failed: job_id=%s exception_type=%s",
            job_id,
            type(error).__name__,
        )


def _gallery_change(job: IdentityGalleryRematchJob) -> dict[str, object]:
    decision = job.decision
    action = {
        SubjectReferenceDecision.Action.SELECTED: "reference_added",
        SubjectReferenceDecision.Action.UPDATED: "reference_updated",
        SubjectReferenceDecision.Action.REMOVED: "reference_removed",
    }[decision.action]
    return {
        "action": action,
        "observation_id": str(decision.observation_id),
        "previous_subject_id": (
            str(decision.subject_id)
            if decision.action == SubjectReferenceDecision.Action.REMOVED
            else (
                str(decision.previous_subject_id)
                if decision.previous_subject_id
                else None
            )
        ),
        "subject_id": (
            None
            if decision.action == SubjectReferenceDecision.Action.REMOVED
            else str(decision.subject_id)
        ),
        "reference_decision_id": str(decision.pk),
        "gallery_rematch_job_id": str(job.pk),
    }


@transaction.atomic
def _claim_gallery_rematch(job_id: UUID | str) -> IdentityGalleryRematchJob | None:
    job = (
        IdentityGalleryRematchJob.objects.select_for_update()
        .select_related("decision__observation")
        .filter(pk=job_id, status=IdentityGalleryRematchJob.Status.PENDING)
        .first()
    )
    if job is None:
        return None
    job.status = IdentityGalleryRematchJob.Status.RUNNING
    job.lease_expires_at = timezone.now() + timedelta(
        seconds=GALLERY_REMATCH_LEASE_SECONDS
    )
    job.save(update_fields=("status", "lease_expires_at", "updated_at"))
    return job


def _next_item_ids(job: IdentityGalleryRematchJob) -> list[UUID]:
    if job.upper_item_id is None:
        return []
    observations = IdentityObservation.objects.filter(
        modality=job.modality,
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        catalog_item_id__lte=job.upper_item_id,
    )
    if job.cursor_item_id is not None:
        observations = observations.filter(catalog_item_id__gt=job.cursor_item_id)
    return list(
        observations.order_by("catalog_item_id")
        .values_list("catalog_item_id", flat=True)
        .distinct()[:GALLERY_REMATCH_BATCH_SIZE]
    )


def _current_eligible_item_ids(
    job: IdentityGalleryRematchJob,
    item_ids: list[UUID],
) -> set[UUID]:
    observations = []
    for item_id in item_ids:
        item_observations = IdentityObservation.objects.filter(
            catalog_item_id=item_id,
            modality=job.modality,
            quality_status=IdentityObservation.QualityStatus.APPROVED,
        ).select_related("catalog_item")
        if job.modality == IdentityObservation.Modality.FACE:
            item_observations = item_observations.filter(
                ~Q(catalog_item__item_type=CatalogItem.ItemType.VIDEO)
                | Q(provenance__identity_evidence_role=(VIDEO_FACE_AGGREGATE_ROLE))
            )
        observations.extend(
            item_observations.order_by("-created_at", "-pk")[
                :IDENTITY_REMATCH_OBSERVATION_LIMIT
            ]
        )
    current = current_source_observations(observations)
    return {
        observation.catalog_item_id
        for observation in current
        if observation_is_gallery_eligible(observation)
    }


def _already_scheduled(job: IdentityGalleryRematchJob, item_id: UUID) -> bool:
    return ProcessingRun.objects.filter(
        catalog_item_id=item_id,
        configuration__workflow="identity_gallery_rematch",
        configuration__gallery_change__gallery_rematch_job_id=str(job.pk),
    ).exists()


def process_gallery_rematch_batch(
    job_id: UUID | str,
) -> GalleryRematchReport:
    """Schedule one bounded page; replay is harmless after an interrupted task."""
    job = _claim_gallery_rematch(job_id)
    if job is None:
        return GalleryRematchReport()

    try:
        item_ids = _next_item_ids(job)
        eligible_item_ids = _current_eligible_item_ids(job, item_ids)
        gallery_change = _gallery_change(job)
        created = 0
        scheduled = 0
        for item_id in item_ids:
            if item_id not in eligible_item_ids:
                continue
            if _already_scheduled(job, item_id):
                scheduled += 1
                continue
            try:
                create_identity_rematch_run(
                    item_id,
                    job.modality,
                    gallery_change=gallery_change,
                )
            except ProcessingError:
                # A missing/replaced source is not current evidence and does not
                # prevent other catalogue items from receiving their rematch.
                continue
            created += 1
            scheduled += 1
    except Exception:
        IdentityGalleryRematchJob.objects.filter(
            pk=job.pk,
            status=IdentityGalleryRematchJob.Status.RUNNING,
        ).update(
            status=IdentityGalleryRematchJob.Status.PENDING,
            lease_expires_at=None,
            failure_count=F("failure_count") + 1,
            updated_at=timezone.now(),
        )
        raise

    completed = len(item_ids) < GALLERY_REMATCH_BATCH_SIZE
    with transaction.atomic():
        current = IdentityGalleryRematchJob.objects.select_for_update().get(pk=job.pk)
        if current.status != IdentityGalleryRematchJob.Status.RUNNING:
            return GalleryRematchReport(
                examined_items=len(item_ids),
                runs_created=created,
            )
        current.cursor_item_id = item_ids[-1] if item_ids else current.cursor_item_id
        # Count already-created runs as well: a worker may have committed a run
        # and died before advancing this cursor. Replay then repairs the count
        # without adding duplicate runs.
        current.runs_created += scheduled
        current.failure_count = 0
        current.lease_expires_at = None
        current.status = (
            IdentityGalleryRematchJob.Status.SUCCEEDED
            if completed
            else IdentityGalleryRematchJob.Status.PENDING
        )
        current.completed_at = timezone.now() if completed else None
        current.save(
            update_fields=(
                "cursor_item_id",
                "runs_created",
                "failure_count",
                "lease_expires_at",
                "status",
                "completed_at",
                "updated_at",
            )
        )
        if not completed:
            enqueue_gallery_rematch_on_commit(current)
    return GalleryRematchReport(
        examined_items=len(item_ids),
        runs_created=created,
        completed=completed,
    )


def reconcile_gallery_rematch_jobs(*, limit: int = 25) -> int:
    """Recover expired/lost scheduler deliveries and dispatch a bounded page."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    now = timezone.now()
    IdentityGalleryRematchJob.objects.filter(
        status=IdentityGalleryRematchJob.Status.RUNNING,
        lease_expires_at__lte=now,
    ).update(
        status=IdentityGalleryRematchJob.Status.PENDING,
        lease_expires_at=None,
        failure_count=F("failure_count") + 1,
        updated_at=now,
    )
    job_ids = list(
        IdentityGalleryRematchJob.objects.filter(
            status=IdentityGalleryRematchJob.Status.PENDING,
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    for job_id in job_ids:
        _dispatch_gallery_rematch_safely(job_id)
    return len(job_ids)

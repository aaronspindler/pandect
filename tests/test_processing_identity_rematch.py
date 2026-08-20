"""Global, durable rematching after closed-gallery reference decisions."""

import uuid
from unittest.mock import patch

import pytest

from pandect.catalog.models import CatalogItem
from pandect.identities.models import (
    IdentityCandidate,
    IdentityGalleryRematchJob,
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
    decide_identity_candidate,
    encode_embedding,
    observation_vector,
    rank_closed_gallery,
)
from pandect.messages.models import Message, TextMessage
from pandect.processing.identity_rematch import (
    create_gallery_rematch_job,
    process_gallery_rematch_batch,
)
from pandect.processing.models import ProcessingRun, ProcessingStage
from pandect.processing.services import create_local_run, source_fingerprint

pytestmark = pytest.mark.django_db


def make_writing_observation(label: str, vector: tuple[float, float]):
    body = " ".join([label] * 200)
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.TEXT_MESSAGE)
    Message.objects.create(catalog_item=item, body=body)
    TextMessage.objects.create(message_id=item.pk)
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.WRITING_IDENTITY)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.SUCCEEDED
    )
    fingerprint = source_fingerprint(item)
    observation = IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.WRITING,
        unit_key="body:0-200",
        source_location={"start": 0, "end": len(body)},
        source_sha256=fingerprint.sha256,
        embedding=encode_embedding(vector),
        embedding_dimension=2,
        feature_space="writing-rematch-v1",
        processor_name="fixture",
        processor_version="1",
        preprocessing_version="1",
        provenance={"processing_run_id": str(run.pk)},
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_details={"fixture": True},
        sample_word_count=200,
    )
    return item, observation, run, stage


def test_gallery_change_invalidates_old_candidates_and_rematches_other_items():
    first_subject = Subject.objects.create(label="First subject")
    second_subject = Subject.objects.create(label="Second subject")
    reference_records = [
        make_writing_observation(f"reference-{index}", (1.0, 0.0)) for index in range(3)
    ]
    query_item, query_observation, query_run, query_stage = make_writing_observation(
        "query", (0.9, 0.1)
    )
    references = [
        SubjectReference.objects.create(
            subject=first_subject,
            observation=observation,
            notes=f"Reference {index}",
        )
        for index, (_item, observation, _run, _stage) in enumerate(reference_records)
    ]
    old_ranking = rank_closed_gallery(
        observation_vector(query_observation),
        current_reference_vectors(references),
    )
    assert [candidate.subject_id for candidate in old_ranking.candidates] == [
        first_subject.pk
    ]
    old_candidate = IdentityCandidate.objects.create(
        candidate_set_id=uuid.uuid4(),
        producing_run=query_run,
        producing_stage=query_stage,
        observation=query_observation,
        subject=first_subject,
        modality=query_observation.modality,
        feature_space=query_observation.feature_space,
        gallery_fingerprint=old_ranking.gallery_fingerprint,
        rank=1,
        similarity_score=old_ranking.candidates[0].similarity_score,
        reference_count=old_ranking.candidates[0].reference_count,
        calibration_revision="uncalibrated-cosine-v1",
    )
    assert current_identity_candidates(query_observation) == (old_candidate,)

    changed = references[0]
    previous_subject = changed.subject
    previous_notes = changed.notes
    changed.subject = second_subject
    changed.notes = "Operator reassigned"
    changed.save(update_fields=("subject", "notes"))
    decision = record_reference_decision(
        changed,
        SubjectReferenceDecision.Action.UPDATED,
        previous_subject=previous_subject,
        previous_notes=previous_notes,
    )
    with patch("pandect.processing.identity_rematch._dispatch_gallery_rematch_safely"):
        job = create_gallery_rematch_job(decision)

    # Candidate compatibility is checked against the live gallery immediately;
    # the operator cannot confirm stale evidence while replacement work waits.
    assert current_identity_candidates(query_observation) == ()
    with pytest.raises(StaleIdentityCandidateError):
        decide_identity_candidate(
            old_candidate.pk,
            IdentityCandidate.Decision.CONFIRMED,
        )

    report = process_gallery_rematch_batch(job.pk)

    job.refresh_from_db()
    assert report.completed is True
    assert report.runs_created == 4
    assert job.status == IdentityGalleryRematchJob.Status.SUCCEEDED
    assert job.runs_created == 4
    replacement_runs = ProcessingRun.objects.filter(
        configuration__workflow="identity_gallery_rematch",
        configuration__gallery_change__gallery_rematch_job_id=str(job.pk),
    )
    assert set(replacement_runs.values_list("catalog_item_id", flat=True)) == {
        query_item.pk,
        *(item.pk for item, _observation, _run, _stage in reference_records),
    }
    query_replacement = replacement_runs.get(catalog_item=query_item)
    assert query_replacement.configuration["gallery_change"] == {
        "action": "reference_updated",
        "observation_id": str(changed.observation_id),
        "previous_subject_id": str(first_subject.pk),
        "subject_id": str(second_subject.pk),
        "reference_decision_id": str(decision.pk),
        "gallery_rematch_job_id": str(job.pk),
    }

    # Replay after a worker committed runs but died before advancing its cursor
    # reuses those runs and repairs its count instead of duplicating evidence.
    IdentityGalleryRematchJob.objects.filter(pk=job.pk).update(
        status=IdentityGalleryRematchJob.Status.PENDING,
        cursor_item_id=None,
        runs_created=0,
        completed_at=None,
    )
    replay = process_gallery_rematch_batch(job.pk)
    job.refresh_from_db()
    assert replay.runs_created == 0
    assert job.runs_created == 4
    assert replacement_runs.count() == 4

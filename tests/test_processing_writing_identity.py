"""Integration coverage for durable writing observations and gallery rematching."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from pandect.catalog.models import CatalogItem
from pandect.identities.models import (
    IdentityCandidate,
    IdentityObservation,
    Subject,
    SubjectReference,
)
from pandect.identities.services import encode_embedding
from pandect.messages.models import Message, TextMessage
from pandect.processing.execution import execute_stage
from pandect.processing.extractors.text import (
    build_writing_identity_vector,
    extract_message_text,
)
from pandect.processing.models import ProcessingResult, ProcessingStage
from pandect.processing.services import (
    claim_stage,
    complete_stage,
    create_identity_rematch_run,
    create_local_run,
    enqueue_ready_stages,
    source_fingerprint,
)
from pandect.videos.models import Video

pytestmark = pytest.mark.django_db


def make_message(body: str) -> CatalogItem:
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.TEXT_MESSAGE)
    message = Message.objects.create(catalog_item=item, body=body)
    TextMessage.objects.create(message=message)
    return item


def make_approved_observation(item: CatalogItem, body: str) -> IdentityObservation:
    feature = build_writing_identity_vector(extract_message_text(body))
    fingerprint = source_fingerprint(item)
    source_run = create_local_run(item, enqueue=False)
    return IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.WRITING,
        unit_key="body",
        source_location={"start": 0, "end": len(body)},
        source_sha256=fingerprint.sha256,
        embedding=encode_embedding(feature["vector"]),
        embedding_dimension=feature["dimension"],
        feature_space=feature["feature_space"],
        processor_name="pandect.writing-profile",
        processor_version="1",
        preprocessing_version="writing-profile-v1",
        provenance={
            "fixture": True,
            "processing_run_id": str(source_run.pk),
        },
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=1.0,
        quality_details={"fixture": True},
        sample_word_count=feature["word_count"],
    )


def claim(stage: ProcessingStage) -> ProcessingStage:
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.QUEUED
    )
    claimed = claim_stage(stage.pk)
    assert claimed is not None
    return claimed


def test_writing_observation_is_committed_before_dependent_gallery_ranking():
    reference_body = " ".join(["characteristic wording"] * 100)
    subject = Subject.objects.create(label="Closed gallery subject")
    for _index in range(3):
        reference_item = make_message(reference_body)
        observation = make_approved_observation(reference_item, reference_body)
        SubjectReference.objects.create(subject=subject, observation=observation)

    query_body = " ".join(["characteristic wording"] * 30)
    query_item = make_message(query_body)
    run = create_local_run(query_item, enqueue=False)
    style_stage = claim(run.stages.get(kind=ProcessingStage.Kind.WRITING_STYLE))

    style_outcome = execute_stage(style_stage)

    assert len(style_outcome.identity_observations) == 1
    assert not IdentityObservation.objects.filter(catalog_item=query_item).exists()
    assert complete_stage(
        style_stage.pk,
        style_outcome,
        expected_attempt=style_stage.attempts,
        observed_source_fingerprint=run.source_fingerprint,
    )
    observation = IdentityObservation.objects.get(catalog_item=query_item)
    assert observation.provenance["processing_run_id"] == str(run.pk)

    identity_stage = claim(run.stages.get(kind=ProcessingStage.Kind.WRITING_IDENTITY))
    identity_outcome = execute_stage(identity_stage)
    result = identity_outcome.results[0]
    ranked = result.data["observations"][0]

    assert result.result_kind == ProcessingResult.Kind.AUTHORSHIP_CANDIDATES
    assert ranked["unknown"] is True
    assert ranked["candidates"] == [
        {
            "subject_id": str(subject.pk),
            "rank": 1,
            "similarity": pytest.approx(1.0, abs=0.001),
            "reference_count": 3,
        }
    ]
    assert "embedding" not in str(result.data).lower()
    assert "vector" not in str(result.data).lower()

    assert complete_stage(
        identity_stage.pk,
        identity_outcome,
        expected_attempt=identity_stage.attempts,
        observed_source_fingerprint=run.source_fingerprint,
    )
    candidate = IdentityCandidate.objects.get(observation=observation)
    assert candidate.subject == subject
    assert candidate.rank == 1
    assert candidate.reference_count == 3


def test_identity_rematch_run_is_bounded_dependency_free_and_versioned():
    body = " ".join(["durable writing evidence"] * 20)
    item = make_message(body)
    make_approved_observation(item, body)

    run = create_identity_rematch_run(
        item,
        IdentityObservation.Modality.WRITING,
        gallery_change={"action": "reference_removed", "subject_id": None},
        enqueue=False,
    )

    assert run is not None
    assert {
        key: value
        for key, value in run.configuration.items()
        if key != "local_processing_snapshot"
    } == {
        "workflow": "identity_gallery_rematch",
        "modality": "writing",
        "observation_limit": 100,
        "gallery_change": {"action": "reference_removed", "subject_id": None},
    }
    snapshot = run.configuration["local_processing_snapshot"]
    assert snapshot["item_type"] == CatalogItem.ItemType.TEXT_MESSAGE
    assert [stage["kind"] for stage in snapshot["stages"]] == [
        ProcessingStage.Kind.WRITING_IDENTITY
    ]
    assert list(run.stages.values_list("kind", flat=True)) == [
        ProcessingStage.Kind.WRITING_IDENTITY
    ]

    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=SimpleNamespace(id="identity-rematch-task"),
    ) as dispatch:
        assert enqueue_ready_stages(run.pk) == 1

    stage = run.stages.get()
    assert stage.status == ProcessingStage.Status.QUEUED
    dispatch.assert_called_once_with(stage.pk)


def test_identity_rematch_does_not_rank_stale_observations_after_source_change():
    body = " ".join(["original words"] * 20)
    item = make_message(body)
    make_approved_observation(item, body)
    item.message.body = "replacement body with a different fingerprint"
    item.message.save()

    run = create_identity_rematch_run(
        item,
        IdentityObservation.Modality.WRITING,
        enqueue=False,
    )

    assert run is not None
    stage = claim(run.stages.get())
    outcome = execute_stage(stage)
    assert outcome.results[0].data == {
        "candidates": [],
        "unknown": True,
        "unknown_label": "Unknown / not in gallery",
        "reason": "no_observation",
    }


@pytest.mark.parametrize(
    ("modality", "source_location", "sample_duration_ms"),
    (
        (
            IdentityObservation.Modality.FACE,
            {"bbox": [1, 2, 30, 40], "frame_index": 3, "timestamp_ms": 1500},
            None,
        ),
        (
            IdentityObservation.Modality.VOICE,
            {"start_ms": 1000, "end_ms": 4000, "stream_index": 1},
            3000,
        ),
    ),
)
def test_identity_rematch_uses_canonical_run_provenance_for_derived_observations(
    modality,
    source_location,
    sample_duration_ms,
):
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.VIDEO)
    Video.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("source.mp4", b"canonical-video-source"),
    )
    source_run = create_local_run(item, enqueue=False)
    derivative_sha256 = "d" * 64
    observation = IdentityObservation.objects.create(
        catalog_item=item,
        modality=modality,
        unit_key="derived:001",
        source_location=source_location,
        source_sha256=derivative_sha256,
        embedding=encode_embedding((1.0, 0.0)),
        embedding_dimension=2,
        feature_space=f"{modality}-fixture-v1",
        processor_name="pandect.test-derived-observation",
        processor_version="1",
        preprocessing_version="1",
        provenance={
            "processing_run_id": str(source_run.pk),
            **(
                {"identity_evidence_role": "aggregate_face_track"}
                if modality == IdentityObservation.Modality.FACE
                else {}
            ),
        },
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=0.9,
        quality_details={"fixture": True},
        sample_duration_ms=sample_duration_ms,
    )

    rematch_run = create_identity_rematch_run(item, modality, enqueue=False)
    outcome = execute_stage(claim(rematch_run.stages.get()))

    assert outcome.results[0].data["observations"][0]["observation_id"] == str(
        observation.pk
    )
    assert observation.source_sha256 == derivative_sha256


def test_identity_rematch_rejects_derived_observation_from_other_canonical_source():
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.VIDEO)
    Video.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("source.mp4", b"current-video-source"),
    )
    stale_run = create_local_run(item, enqueue=False)
    stale_run.source_fingerprint = "a" * 64
    stale_run.save(update_fields=("source_fingerprint",))
    IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.FACE,
        unit_key="track:001",
        source_location={"bbox": [1, 2, 30, 40], "frame_index": 2},
        source_sha256="d" * 64,
        embedding=encode_embedding((1.0, 0.0)),
        embedding_dimension=2,
        feature_space="face-fixture-v1",
        processor_name="pandect.test-derived-observation",
        processor_version="1",
        preprocessing_version="1",
        provenance={"processing_run_id": str(stale_run.pk)},
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=0.9,
        quality_details={"fixture": True},
    )

    rematch_run = create_identity_rematch_run(
        item,
        IdentityObservation.Modality.FACE,
        enqueue=False,
    )
    outcome = execute_stage(claim(rematch_run.stages.get()))

    assert outcome.results[0].data == {
        "candidates": [],
        "unknown": True,
        "unknown_label": "Unknown / not in gallery",
        "reason": "no_observation",
    }

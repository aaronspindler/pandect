"""Video face tracks remain one closed-gallery observation across frames."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from pandect.catalog.models import CatalogItem
from pandect.identities.models import (
    IdentityObservation,
    Subject,
    SubjectReference,
)
from pandect.identities.services import (
    VIDEO_FACE_AGGREGATE_ROLE,
    VIDEO_FACE_FRAME_ROLE,
    encode_embedding,
    observation_vector,
    rank_closed_gallery,
    reference_vector,
)
from pandect.processing.execution import _face_identity
from pandect.processing.extractors.hashing import FileFingerprint
from pandect.processing.models import ProcessingRun, ProcessingStage

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def canonical_source_fixture(monkeypatch):
    monkeypatch.setattr(
        "pandect.processing.services.source_fingerprint",
        lambda _item: FileFingerprint(sha256="a" * 64, size_bytes=10),
    )


def _run(item: CatalogItem) -> ProcessingRun:
    return ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.AUTOMATIC,
        source_fingerprint="a" * 64,
        source_size=10,
        processor_version="1",
        status=ProcessingRun.Status.RUNNING,
    )


def _stage(run: ProcessingRun, unit_key: str) -> ProcessingStage:
    return ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key=unit_key,
        status=ProcessingStage.Status.SUCCEEDED,
        processor_name="pandect.video-face-track",
    )


def _face_observation(
    item: CatalogItem,
    *,
    unit_key: str,
    role: str | None = None,
    run: ProcessingRun | None = None,
    stage: ProcessingStage | None = None,
    frame_index: int | None = None,
) -> IdentityObservation:
    provenance = {"fixture": True}
    if role is not None:
        provenance["identity_evidence_role"] = role
    if run is not None:
        provenance["processing_run_id"] = str(run.pk)
    if stage is not None:
        provenance["processing_stage_id"] = str(stage.pk)
    location = {"bbox": [1, 2, 30, 40]}
    if frame_index is not None:
        location.update(
            {"frame_index": frame_index, "timestamp_ms": frame_index * 1000}
        )
    return IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.FACE,
        unit_key=unit_key,
        source_location=location,
        source_sha256="b" * 64,
        embedding=encode_embedding((1.0, 0.0)),
        embedding_dimension=2,
        feature_space="opencv-sface:test",
        processor_name="pandect.video-face-track",
        processor_version="1",
        preprocessing_version="tracking-v1",
        provenance=provenance,
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=0.9,
        quality_details={"quality_assessment": "usable"},
    )


def _eligible_face_references(subject: Subject) -> list[SubjectReference]:
    references = []
    for index in range(2):
        item = CatalogItem.objects.create(
            item_type=CatalogItem.ItemType.IMAGE,
            title=f"Reference image {index}",
        )
        run = _run(item)
        observation = _face_observation(
            item,
            unit_key=f"face:{index}",
            run=run,
        )
        references.append(
            SubjectReference.objects.create(subject=subject, observation=observation)
        )
    return references


def test_face_identity_ranks_only_one_aggregate_track_for_two_frame_children():
    item = CatalogItem.objects.create(
        item_type=CatalogItem.ItemType.VIDEO,
        title="Two-frame video",
    )
    run = _run(item)
    for index in range(2):
        child_stage = _stage(run, f"frame:{index:03d}")
        _face_observation(
            item,
            unit_key="track:001",
            role=VIDEO_FACE_FRAME_ROLE,
            run=run,
            stage=child_stage,
            frame_index=index,
        )
    aggregate_stage = _stage(run, "aggregate")
    aggregate = _face_observation(
        item,
        unit_key="track:001",
        role=VIDEO_FACE_AGGREGATE_ROLE,
        run=run,
        stage=aggregate_stage,
        frame_index=1,
    )
    subject = Subject.objects.create(label="Known subject")
    _eligible_face_references(subject)
    identity_stage = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.FACE_IDENTITY,
        status=ProcessingStage.Status.RUNNING,
        processor_name="pandect.closed-gallery",
    )

    outcome = _face_identity(identity_stage)

    observations = outcome.results[0].data["observations"]
    assert [value["observation_id"] for value in observations] == [str(aggregate.pk)]
    assert len(outcome.identity_candidates) == 1
    assert outcome.identity_candidates[0].observation_id == aggregate.pk


def test_frame_children_are_not_reference_options_or_counted_for_eligibility(client):
    item = CatalogItem.objects.create(
        item_type=CatalogItem.ItemType.VIDEO,
        title="Tracked face",
    )
    run = _run(item)
    child_stage = _stage(run, "frame:000")
    child = _face_observation(
        item,
        unit_key="track:001",
        role=VIDEO_FACE_FRAME_ROLE,
        run=run,
        stage=child_stage,
        frame_index=0,
    )
    aggregate_stage = _stage(run, "aggregate")
    aggregate = _face_observation(
        item,
        unit_key="track:001",
        role=VIDEO_FACE_AGGREGATE_ROLE,
        run=run,
        stage=aggregate_stage,
        frame_index=0,
    )
    subject = Subject.objects.create(label="Local subject")

    response = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    assert response.status_code == 200
    content = response.content.decode()
    assert str(aggregate.pk)[:8] in content
    assert str(child.pk)[:8] not in content

    rejected = client.post(
        reverse("catalogue-item-add-reference", args=(item.pk, child.pk)),
        {"subject": subject.pk, "notes": "Must remain internal"},
    )
    assert rejected.status_code == 400
    assert not SubjectReference.objects.filter(observation=child).exists()

    # Even a legacy row inserted without model validation cannot manufacture
    # the two distinct face references required to make a subject eligible.
    reference_image = CatalogItem.objects.create(
        item_type=CatalogItem.ItemType.IMAGE,
        title="One valid reference",
    )
    valid_reference = SubjectReference.objects.create(
        subject=subject,
        observation=_face_observation(reference_image, unit_key="face:001"),
    )
    legacy_frame_reference = SubjectReference(subject=subject, observation=child)
    SubjectReference.objects.bulk_create([legacy_frame_reference])

    ranked = rank_closed_gallery(
        observation_vector(aggregate),
        [
            reference_vector(valid_reference),
            reference_vector(
                SubjectReference.objects.select_related("observation").get(
                    observation=child
                )
            ),
        ],
    )
    assert ranked.candidates == ()
    assert ranked.reason == "no_eligible_gallery_subjects"


def test_subject_reference_model_rejects_video_frame_child_directly():
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.VIDEO)
    run = _run(item)
    child = _face_observation(
        item,
        unit_key="track:001",
        role=VIDEO_FACE_FRAME_ROLE,
        run=run,
        stage=_stage(run, "frame:000"),
        frame_index=0,
    )

    with pytest.raises(ValidationError, match="aggregate video face tracks"):
        SubjectReference.objects.create(
            subject=Subject.objects.create(label="Subject"),
            observation=child,
        )

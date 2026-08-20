import math
import uuid
from datetime import datetime

import pytest
from django.core.exceptions import ValidationError

from pandect.catalog.models import CatalogItem
from pandect.identities.models import (
    IdentityAssertion,
    IdentityCandidate,
    IdentityCandidateDecision,
    IdentityObservation,
    Subject,
    SubjectReference,
)
from pandect.identities.services import (
    ObservationVector,
    ReferenceVector,
    cosine_similarity,
    decide_identity_candidate,
    decode_embedding,
    encode_embedding,
    observation_vector,
    rank_closed_gallery,
    reference_vector,
)
from pandect.processing.extractors.hashing import FileFingerprint
from pandect.processing.models import ProcessingRun, ProcessingStage

SOURCE_SHA256 = "a" * 64
MODEL_SHA256 = "b" * 64
GALLERY_SHA256 = "c" * 64


@pytest.fixture(autouse=True)
def canonical_source_fixture(monkeypatch):
    """Give model-only identity fixtures a stable canonical source snapshot."""
    monkeypatch.setattr(
        "pandect.processing.services.source_fingerprint",
        lambda _item: FileFingerprint(
            sha256=SOURCE_SHA256,
            size_bytes=1,
        ),
    )


def create_observation(
    *,
    item=None,
    modality=IdentityObservation.Modality.FACE,
    values=(1.0, 0.0, 0.0),
    feature_space="test-space-v1",
    quality_status=IdentityObservation.QualityStatus.APPROVED,
    source_location=None,
    sample_duration_ms=None,
    sample_word_count=None,
):
    if item is None:
        item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    source_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.AUTOMATIC,
        scope=ProcessingRun.Scope.LOCAL,
        source_fingerprint=SOURCE_SHA256,
        source_size=1,
        processor_version="1",
        status=ProcessingRun.Status.SUCCEEDED,
    )
    if modality == IdentityObservation.Modality.FACE:
        source_location = source_location or {"bbox": [1, 2, 30, 40]}
    elif modality == IdentityObservation.Modality.VOICE:
        sample_duration_ms = sample_duration_ms or 10_000
        source_location = source_location or {
            "start_ms": 0,
            "end_ms": sample_duration_ms,
        }
    else:
        sample_word_count = sample_word_count or 200
        source_location = source_location or {"start": 0, "end": 1200}
    return IdentityObservation.objects.create(
        catalog_item=item,
        modality=modality,
        unit_key=f"{modality}:{uuid.uuid4()}",
        source_location=source_location,
        source_sha256=SOURCE_SHA256,
        embedding=encode_embedding(values),
        embedding_dimension=len(values),
        feature_space=feature_space,
        processor_name="identity-test-processor",
        processor_version="1.0",
        model_identifier="local/test-model",
        model_version="1",
        model_sha256=MODEL_SHA256,
        preprocessing_version="preprocess-v1",
        provenance={
            "runtime": "test",
            "processing_run_id": str(source_run.pk),
        },
        quality_status=quality_status,
        quality_score=0.9,
        quality_details={"signal": "clear"},
        sample_duration_ms=sample_duration_ms,
        sample_word_count=sample_word_count,
    )


def pure_observation(
    *,
    modality=IdentityObservation.Modality.FACE,
    feature_space="space-v1",
    values=(1.0, 0.0),
    quality_status=IdentityObservation.QualityStatus.APPROVED,
):
    return ObservationVector(
        observation_id=uuid.uuid4(),
        catalog_item_id=uuid.uuid4(),
        modality=modality,
        feature_space=feature_space,
        values=tuple(values),
        quality_status=quality_status,
    )


def pure_reference(
    subject_id,
    *,
    modality=IdentityObservation.Modality.FACE,
    feature_space="space-v1",
    values=(1.0, 0.0),
    catalog_item_id=None,
    duration_ms=None,
    word_count=None,
    unit_key="",
    source_location=None,
    quality_status=IdentityObservation.QualityStatus.APPROVED,
):
    return ReferenceVector(
        subject_id=subject_id,
        observation_id=uuid.uuid4(),
        catalog_item_id=catalog_item_id or uuid.uuid4(),
        modality=modality,
        feature_space=feature_space,
        values=tuple(values),
        quality_status=quality_status,
        unit_key=unit_key,
        source_location=source_location or {},
        sample_duration_ms=duration_ms,
        sample_word_count=word_count,
    )


def create_candidate(observation, subject, *, rank, candidate_set_id=None):
    candidate_set_id = candidate_set_id or uuid.uuid4()
    existing = IdentityCandidate.objects.filter(
        candidate_set_id=candidate_set_id,
        observation=observation,
    ).first()
    if existing:
        run = existing.producing_run
        stage = existing.producing_stage
    else:
        run = ProcessingRun.objects.create(
            catalog_item=observation.catalog_item,
            trigger=ProcessingRun.Trigger.MANUAL,
            source_fingerprint=SOURCE_SHA256,
            source_size=1,
            processor_version="1",
            status=ProcessingRun.Status.SUCCEEDED,
        )
        stage = ProcessingStage.objects.create(
            run=run,
            kind={
                IdentityObservation.Modality.FACE: ProcessingStage.Kind.FACE_IDENTITY,
                IdentityObservation.Modality.VOICE: (
                    ProcessingStage.Kind.VOICE_IDENTITY
                ),
                IdentityObservation.Modality.WRITING: (
                    ProcessingStage.Kind.WRITING_IDENTITY
                ),
            }[observation.modality],
            status=ProcessingStage.Status.SUCCEEDED,
            processor_name="identity-test",
        )
    fingerprint = rank_closed_gallery(
        observation_vector(observation), []
    ).gallery_fingerprint
    return IdentityCandidate.objects.create(
        candidate_set_id=candidate_set_id,
        producing_run=run,
        producing_stage=stage,
        observation=observation,
        subject=subject,
        modality=observation.modality,
        feature_space=observation.feature_space,
        gallery_fingerprint=fingerprint,
        rank=rank,
        similarity_score=1.0 - (rank / 10),
        reference_count=2,
        calibration_revision="raw-cosine-v1",
    )


@pytest.mark.django_db
def test_subject_requires_a_meaningful_operator_label():
    with pytest.raises(ValidationError, match="blank"):
        Subject.objects.create(label="  \n")

    subject = Subject.objects.create(label="Known person", notes="Local context")

    assert str(subject) == "Known person"


@pytest.mark.django_db
def test_observation_preserves_opaque_vector_provenance_quality_and_location():
    observation = create_observation(values=(0.25, -0.5, 1.0))
    observation.refresh_from_db()

    assert bytes(observation.embedding) == encode_embedding((0.25, -0.5, 1.0))
    assert decode_embedding(observation.embedding, 3) == pytest.approx(
        (0.25, -0.5, 1.0)
    )
    assert observation.source_location == {"bbox": [1, 2, 30, 40]}
    assert observation.provenance["runtime"] == "test"
    assert observation.provenance["processing_run_id"]
    assert observation.quality_details == {"signal": "clear"}
    assert observation.model_sha256 == MODEL_SHA256
    assert IdentityObservation._meta.get_field("embedding").editable is False
    assert encode_embedding((0.25, -0.5, 1.0)).hex() not in str(observation)
    assert observation_vector(observation).values == pytest.approx((0.25, -0.5, 1.0))


@pytest.mark.django_db
def test_observation_is_append_only_through_instance_and_queryset_writes():
    observation = create_observation()
    observation.provenance = {"runtime": "rewritten"}

    with pytest.raises(TypeError, match="append-only"):
        observation.save()
    with pytest.raises(TypeError, match="append-only"):
        observation.delete()
    with pytest.raises(TypeError, match="append-only"):
        IdentityObservation.objects.filter(pk=observation.pk).update(
            provenance={"runtime": "rewritten"}
        )
    with pytest.raises(TypeError, match="append-only"):
        IdentityObservation.objects.bulk_update([observation], ["provenance"])
    with pytest.raises(TypeError, match="append-only"):
        IdentityObservation.objects.filter(pk=observation.pk).delete()

    stored = IdentityObservation.objects.get(pk=observation.pk)
    assert stored.provenance["runtime"] == "test"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("modality", "location", "duration", "words", "message"),
    [
        (
            IdentityObservation.Modality.FACE,
            {"bbox": [0, 0, 0, 10]},
            None,
            None,
            "bounding-box",
        ),
        (
            IdentityObservation.Modality.VOICE,
            {"start_ms": 100, "end_ms": 500},
            500,
            None,
            "must equal",
        ),
        (
            IdentityObservation.Modality.WRITING,
            {"start": 20, "end": 10},
            None,
            5,
            "start < end",
        ),
    ],
)
def test_observation_rejects_invalid_modality_specific_source_locations(
    modality,
    location,
    duration,
    words,
    message,
):
    with pytest.raises(ValidationError, match=message):
        create_observation(
            modality=modality,
            source_location=location,
            sample_duration_ms=duration,
            sample_word_count=words,
        )


@pytest.mark.django_db
def test_observation_rejects_invalid_binary_shape_nonfinite_values_and_quality():
    fields = {
        "catalog_item": CatalogItem.objects.create(
            item_type=CatalogItem.ItemType.IMAGE
        ),
        "modality": IdentityObservation.Modality.FACE,
        "unit_key": "face:1",
        "source_location": {"bbox": [0, 0, 10, 10]},
        "source_sha256": SOURCE_SHA256,
        "embedding": encode_embedding((1.0, 0.0)),
        "embedding_dimension": 3,
        "feature_space": "space-v1",
        "processor_name": "test",
        "processor_version": "1",
        "preprocessing_version": "1",
        "quality_status": IdentityObservation.QualityStatus.APPROVED,
    }
    with pytest.raises(ValidationError, match="32-bit float"):
        IdentityObservation.objects.create(**fields)

    fields["embedding"] = b"\x7f\xc0\x00\x00" + encode_embedding((0.0, 0.0))
    with pytest.raises(ValidationError, match="finite"):
        IdentityObservation.objects.create(**fields)

    fields["embedding"] = encode_embedding((1.0, 0.0, 0.0))
    fields["quality_score"] = 1.1
    with pytest.raises(ValidationError):
        IdentityObservation.objects.create(**fields)


@pytest.mark.django_db
def test_gallery_reference_requires_approved_quality_and_one_subject():
    unassessed = create_observation(
        quality_status=IdentityObservation.QualityStatus.UNASSESSED
    )
    first = Subject.objects.create(label="First")
    second = Subject.objects.create(label="Second")

    with pytest.raises(ValidationError, match="quality-approved"):
        SubjectReference.objects.create(subject=first, observation=unassessed)

    approved = create_observation()
    selected = SubjectReference.objects.create(subject=first, observation=approved)
    with pytest.raises(ValidationError):
        SubjectReference.objects.create(subject=second, observation=approved)

    assert reference_vector(selected).subject_id == first.pk


def test_cosine_similarity_validates_shape_and_zero_vectors():
    assert cosine_similarity((1, 0), (1, 0)) == pytest.approx(1.0)
    assert cosine_similarity((1, 0), (-1, 0)) == pytest.approx(-1.0)
    with pytest.raises(ValueError, match="dimensions"):
        cosine_similarity((1, 0), (1, 0, 0))
    with pytest.raises(ValueError, match="zero vector"):
        cosine_similarity((0, 0), (1, 0))
    with pytest.raises(ValueError, match="finite"):
        encode_embedding((math.nan, 0))


def test_face_ranking_is_version_compatible_stable_top_five_plus_unknown():
    query = pure_observation()
    subject_ids = [uuid.uuid4() for _ in range(7)]
    references = []
    for subject_index, subject_id in enumerate(subject_ids):
        references.extend(
            [
                pure_reference(
                    subject_id,
                    values=(1.0, subject_index * 0.08 + offset),
                )
                for offset in (0.001, 0.002)
            ]
        )
    incompatible_subject = uuid.uuid4()
    references.extend(
        [
            pure_reference(incompatible_subject, feature_space="space-v2"),
            pure_reference(incompatible_subject, feature_space="space-v2"),
        ]
    )

    result = rank_closed_gallery(query, references)
    reversed_result = rank_closed_gallery(query, reversed(references))

    assert [candidate.subject_id for candidate in result.candidates] == subject_ids[:5]
    assert [candidate.rank for candidate in result.candidates] == [1, 2, 3, 4, 5]
    assert incompatible_subject not in {
        candidate.subject_id for candidate in result.candidates
    }
    assert result.unknown.label == "Unknown / not in gallery"
    assert result.gallery_fingerprint == reversed_result.gallery_fingerprint


def test_voice_gallery_requires_three_clips_totalling_thirty_seconds():
    query = pure_observation(modality=IdentityObservation.Modality.VOICE)
    eligible = uuid.uuid4()
    too_few = uuid.uuid4()
    too_short = uuid.uuid4()
    references = [
        *[
            pure_reference(
                eligible,
                modality=IdentityObservation.Modality.VOICE,
                duration_ms=10_000,
            )
            for _ in range(3)
        ],
        *[
            pure_reference(
                too_few,
                modality=IdentityObservation.Modality.VOICE,
                duration_ms=15_000,
            )
            for _ in range(2)
        ],
        *[
            pure_reference(
                too_short,
                modality=IdentityObservation.Modality.VOICE,
                duration_ms=9_999,
            )
            for _ in range(3)
        ],
    ]

    result = rank_closed_gallery(query, references)

    assert [candidate.subject_id for candidate in result.candidates] == [eligible]
    assert result.candidates[0].reference_count == 3


def test_face_reruns_of_the_same_source_unit_count_once():
    query = pure_observation()
    subject_id = uuid.uuid4()
    source_item = uuid.uuid4()
    references = [
        pure_reference(
            subject_id,
            catalog_item_id=source_item,
            unit_key="face:001",
        )
        for _ in range(2)
    ]

    result = rank_closed_gallery(query, references)

    assert result.candidates == ()


def test_voice_overlapping_chunks_do_not_create_distinct_clips_or_duration():
    query = pure_observation(modality=IdentityObservation.Modality.VOICE)
    subject_id = uuid.uuid4()
    source_item = uuid.uuid4()
    intervals = ((0, 15_000), (14_000, 30_000), (30_000, 45_000))
    references = [
        pure_reference(
            subject_id,
            modality=IdentityObservation.Modality.VOICE,
            catalog_item_id=source_item,
            duration_ms=end - start,
            unit_key=f"chunk:{index}",
            source_location={
                "start_ms": start,
                "end_ms": end,
                "stream_index": 0,
            },
        )
        for index, (start, end) in enumerate(intervals)
    ]

    result = rank_closed_gallery(query, references)

    assert result.candidates == ()


def test_writing_gallery_requires_three_messages_and_five_hundred_words():
    query = pure_observation(modality=IdentityObservation.Modality.WRITING)
    eligible = uuid.uuid4()
    repeated_message = uuid.uuid4()
    too_short = uuid.uuid4()
    shared_item = uuid.uuid4()
    eligible_references = [
        pure_reference(
            eligible,
            modality=IdentityObservation.Modality.WRITING,
            word_count=count,
        )
        for count in (200, 150, 150)
    ]
    repeated_references = [
        pure_reference(
            repeated_message,
            modality=IdentityObservation.Modality.WRITING,
            catalog_item_id=shared_item if index < 2 else uuid.uuid4(),
            word_count=200,
        )
        for index in range(3)
    ]
    short_references = [
        pure_reference(
            too_short,
            modality=IdentityObservation.Modality.WRITING,
            word_count=166,
        )
        for _ in range(3)
    ]

    result = rank_closed_gallery(
        query,
        [*eligible_references, *repeated_references, *short_references],
    )

    assert [candidate.subject_id for candidate in result.candidates] == [eligible]


def test_writing_reruns_do_not_double_count_one_message_word_total():
    query = pure_observation(modality=IdentityObservation.Modality.WRITING)
    subject_id = uuid.uuid4()
    long_message = uuid.uuid4()
    references = [
        pure_reference(
            subject_id,
            modality=IdentityObservation.Modality.WRITING,
            catalog_item_id=long_message,
            word_count=400,
        ),
        pure_reference(
            subject_id,
            modality=IdentityObservation.Modality.WRITING,
            catalog_item_id=long_message,
            word_count=400,
        ),
        pure_reference(
            subject_id,
            modality=IdentityObservation.Modality.WRITING,
            word_count=40,
        ),
        pure_reference(
            subject_id,
            modality=IdentityObservation.Modality.WRITING,
            word_count=40,
        ),
    ]

    result = rank_closed_gallery(query, references)

    assert result.candidates == ()


def test_unapproved_query_returns_only_unknown_with_an_actionable_reason():
    query = pure_observation(quality_status=IdentityObservation.QualityStatus.REJECTED)
    subject_id = uuid.uuid4()

    result = rank_closed_gallery(
        query,
        [pure_reference(subject_id), pure_reference(subject_id)],
    )

    assert result.candidates == ()
    assert result.unknown.label
    assert result.reason == "query_quality_not_approved"


@pytest.mark.django_db
def test_candidate_enforces_snapshot_rank_score_and_version_constraints():
    observation = create_observation()
    first = Subject.objects.create(label="First")
    second = Subject.objects.create(label="Second")
    candidate_set_id = uuid.uuid4()
    create_candidate(
        observation,
        first,
        rank=1,
        candidate_set_id=candidate_set_id,
    )

    with pytest.raises(ValidationError):
        create_candidate(
            observation,
            second,
            rank=1,
            candidate_set_id=candidate_set_id,
        )
    with pytest.raises(ValidationError, match="modality"):
        IdentityCandidate.objects.create(
            candidate_set_id=uuid.uuid4(),
            observation=observation,
            subject=second,
            modality=IdentityObservation.Modality.VOICE,
            feature_space=observation.feature_space,
            gallery_fingerprint=GALLERY_SHA256,
            rank=1,
            similarity_score=0.5,
            reference_count=2,
            calibration_revision="raw-cosine-v1",
        )
    with pytest.raises(ValidationError):
        create_candidate(observation, second, rank=6)

    persisted = IdentityCandidate.objects.get(
        candidate_set_id=candidate_set_id,
        subject=first,
    )
    persisted.similarity_score = 0.1
    with pytest.raises(TypeError, match="immutable"):
        persisted.save()


@pytest.mark.django_db
def test_candidate_decisions_preserve_assertion_history_and_one_active_subject():
    observation = create_observation()
    first_subject = Subject.objects.create(label="First")
    second_subject = Subject.objects.create(label="Second")
    candidate_set_id = uuid.uuid4()
    first = create_candidate(
        observation,
        first_subject,
        rank=1,
        candidate_set_id=candidate_set_id,
    )
    second = create_candidate(
        observation,
        second_subject,
        rank=2,
        candidate_set_id=candidate_set_id,
    )

    first_assertion = decide_identity_candidate(
        first.pk,
        IdentityCandidate.Decision.CONFIRMED,
        note="Operator recognized the face.",
    )
    same_assertion = decide_identity_candidate(
        first.pk,
        IdentityCandidate.Decision.CONFIRMED,
    )
    second_assertion = decide_identity_candidate(
        second.pk,
        IdentityCandidate.Decision.CONFIRMED,
    )

    assert same_assertion.pk == first_assertion.pk
    first.refresh_from_db()
    first_assertion.refresh_from_db()
    second.refresh_from_db()
    assert first.decision == IdentityCandidate.Decision.REJECTED
    assert first_assertion.status == IdentityAssertion.Status.RETRACTED
    assert first_assertion.retracted_at is not None
    assert second.decision == IdentityCandidate.Decision.CONFIRMED
    assert second_assertion.status == IdentityAssertion.Status.ACTIVE
    assert (
        IdentityAssertion.objects.filter(
            observation=observation,
            status=IdentityAssertion.Status.ACTIVE,
        ).count()
        == 1
    )
    history = list(IdentityCandidateDecision.objects.order_by("decided_at", "id"))
    assert (
        sum(event.decision == IdentityCandidate.Decision.CONFIRMED for event in history)
        == 3
    )
    assert (
        sum(event.decision == IdentityCandidate.Decision.REJECTED for event in history)
        == 1
    )
    assert any(
        event.origin == IdentityCandidateDecision.Origin.SUPERSESSION
        for event in history
    )

    decide_identity_candidate(
        second.pk,
        IdentityCandidate.Decision.REJECTED,
        note="Operator corrected the association.",
    )
    second.refresh_from_db()
    second_assertion.refresh_from_db()
    assert second.decision == IdentityCandidate.Decision.REJECTED
    assert second_assertion.status == IdentityAssertion.Status.RETRACTED
    assert second_assertion.retraction_note == "Operator corrected the association."
    assert (
        IdentityAssertion.objects.filter(
            observation=observation,
            status=IdentityAssertion.Status.ACTIVE,
        ).count()
        == 0
    )
    assert IdentityAssertion.objects.filter(observation=observation).count() == 2
    assert (
        IdentityCandidateDecision.objects.filter(
            candidate=second,
            decision=IdentityCandidate.Decision.REJECTED,
            origin=IdentityCandidateDecision.Origin.OPERATOR,
        ).count()
        == 1
    )

    decision_event = IdentityCandidateDecision.objects.first()
    decision_event.note = "Rewritten"
    with pytest.raises(TypeError, match="append-only"):
        decision_event.save()
    with pytest.raises(TypeError, match="append-only"):
        decision_event.delete()
    with pytest.raises(TypeError, match="append-only"):
        IdentityCandidateDecision.objects.filter(pk=decision_event.pk).update(
            note="Rewritten"
        )
    with pytest.raises(TypeError, match="append-only"):
        IdentityCandidateDecision.objects.bulk_update([decision_event], ["note"])
    with pytest.raises(TypeError, match="append-only"):
        IdentityCandidateDecision.objects.filter(pk=decision_event.pk).delete()


@pytest.mark.django_db
def test_older_candidate_set_is_not_actionable_after_a_newer_compatible_set():
    observation = create_observation()
    first_subject = Subject.objects.create(label="First")
    second_subject = Subject.objects.create(label="Second")
    older = create_candidate(observation, first_subject, rank=1)
    newer = create_candidate(observation, second_subject, rank=1)

    with pytest.raises(ValueError, match="stale"):
        decide_identity_candidate(
            older.pk,
            IdentityCandidate.Decision.CONFIRMED,
        )

    assertion = decide_identity_candidate(
        newer.pk,
        IdentityCandidate.Decision.CONFIRMED,
    )
    assert assertion.subject == second_subject


@pytest.mark.django_db
def test_active_assertion_requires_matching_confirmed_candidate():
    observation = create_observation()
    first = Subject.objects.create(label="First")
    second = Subject.objects.create(label="Second")
    candidate = create_candidate(observation, first, rank=1)

    with pytest.raises(ValidationError, match="confirmed candidate"):
        IdentityAssertion.objects.create(
            candidate=candidate,
            observation=observation,
            subject=first,
        )

    candidate.decision = IdentityCandidate.Decision.CONFIRMED
    candidate.decided_at = datetime(2026, 8, 12, 12, 0)
    with pytest.raises(ValidationError, match="timezone information"):
        candidate.save()

    candidate.decided_at = None
    candidate.decision = IdentityCandidate.Decision.SUGGESTED
    candidate.save()
    with pytest.raises(ValidationError, match="match the candidate"):
        IdentityAssertion.objects.create(
            candidate=candidate,
            observation=observation,
            subject=second,
        )

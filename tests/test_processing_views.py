"""Request tests for durable processing evidence and operator decisions."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from pandect.audio.models import Audio
from pandect.catalog.forms import MessageMetadataForm
from pandect.catalog.models import CatalogItem
from pandect.events.models import Event
from pandect.identities.models import (
    IdentityAssertion,
    IdentityCandidate,
    IdentityCandidateDecision,
    IdentityGalleryRematchJob,
    IdentityObservation,
    Subject,
    SubjectReference,
    SubjectReferenceDecision,
)
from pandect.identities.services import (
    current_reference_vectors,
    encode_embedding,
    observation_vector,
    rank_closed_gallery,
    reference_vector,
)
from pandect.messages.models import (
    CommunicationParticipant,
    ImageMessage,
    Message,
    TextMessage,
)
from pandect.photos.models import Image
from pandect.processing.models import (
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
    TimestampClaim,
    TimestampDecision,
)
from pandect.processing.remote.consent import ConsentManifest
from pandect.processing.remote.followup import FOLLOWUP_NOTICE_VERSION, FollowupPlan
from pandect.processing.remote.workflows import (
    REMOTE_NOTICE_VERSION,
    RemotePlan,
    RemotePlanError,
)
from pandect.processing.services import (
    ProcessingError,
    create_local_run,
    source_fingerprint,
)

pytestmark = pytest.mark.django_db


def make_message_item(body="Original message", *, sender=None):
    item = CatalogItem.objects.create(
        item_type=CatalogItem.ItemType.TEXT_MESSAGE,
        title="Evidence message",
    )
    message = Message.objects.create(catalog_item=item, body=body, sender=sender)
    TextMessage.objects.create(message=message)
    return item


def make_run(item, *, status=ProcessingRun.Status.SUCCEEDED):
    try:
        fingerprint = source_fingerprint(item.pk)
    except ProcessingError:
        source_sha256 = "a" * 64
        source_size = 20
    else:
        source_sha256 = fingerprint.sha256
        source_size = fingerprint.size_bytes
    return ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.AUTOMATIC,
        source_fingerprint=source_sha256,
        source_size=source_size,
        processor_version="1",
        status=status,
    )


def make_stage(run, *, kind=ProcessingStage.Kind.TEXT_EXTRACTION, status=None):
    return ProcessingStage.objects.create(
        run=run,
        kind=kind,
        status=status or ProcessingStage.Status.SUCCEEDED,
        processor_name="fixture",
    )


def make_writing_observation(
    item, *, quality=IdentityObservation.QualityStatus.APPROVED
):
    fingerprint = source_fingerprint(item)
    source_run = ProcessingRun.objects.filter(
        catalog_item=item,
        scope=ProcessingRun.Scope.LOCAL,
        source_fingerprint=fingerprint.sha256,
        source_size=fingerprint.size_bytes,
    ).order_by("-created_at", "-pk").first() or make_run(item)
    return IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.WRITING,
        unit_key="body:0-16",
        source_location={"start": 0, "end": 16},
        source_sha256=fingerprint.sha256,
        embedding=encode_embedding((0.25, 0.75)),
        embedding_dimension=2,
        feature_space="writing-v1",
        processor_name="fixture",
        processor_version="1",
        preprocessing_version="1",
        provenance={
            "fixture": True,
            "processing_run_id": str(source_run.pk),
        },
        quality_status=quality,
        quality_details={"fixture": True},
        sample_word_count=4,
    )


def test_item_detail_separates_four_evidence_tiers_and_escapes_results(client):
    item = make_message_item("Do not execute <script>alert(1)</script>")
    run = make_run(item)
    stage = make_stage(run)
    ProcessingResult.objects.create(
        stage=stage,
        result_kind=ProcessingResult.Kind.TEXT_EXTRACTION,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        data={"found": "<unsafe>"},
        processor_name="local-parser",
        processor_version="1",
    )
    ProcessingResult.objects.create(
        stage=stage,
        result_kind=ProcessingResult.Kind.SEMANTIC_ANALYSIS,
        evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
        text="A hypothesis, not a fact.",
        processor_name="fake-model",
        processor_version="1",
    )
    make_writing_observation(item)

    response = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Canonical and operator facts" in content
    assert "Deterministic extraction" in content
    assert "Model-generated hypotheses" in content
    assert "Operator-confirmed assertions" in content
    assert "Do not execute &lt;script&gt;alert(1)&lt;/script&gt;" in content
    assert "&lt;unsafe&gt;" in content
    assert "A hypothesis, not a fact." in content
    assert "writing-v1" in content
    assert "0.25" not in content
    assert "0.75" not in content


def test_default_detail_keeps_full_local_base_and_layers_current_supplements(client):
    item = make_message_item("One canonical source for layered evidence")
    base_run = make_run(item)
    base_stage = make_stage(base_run)
    ProcessingResult.objects.create(
        stage=base_stage,
        result_kind=ProcessingResult.Kind.TEXT_EXTRACTION,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        text="base-deterministic-marker",
        processor_name="fixture-local",
        processor_version="1",
    )

    remote_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.MANUAL,
        scope=ProcessingRun.Scope.REMOTE,
        source_fingerprint=base_run.source_fingerprint,
        source_size=base_run.source_size,
        processor_version="1",
        status=ProcessingRun.Status.SUCCEEDED,
        configuration={"workflow": "cloudflare-consented-v1"},
    )
    remote_stage = make_stage(
        remote_run,
        kind=ProcessingStage.Kind.REMOTE_SEMANTIC,
    )
    ProcessingResult.objects.create(
        stage=remote_stage,
        result_kind=ProcessingResult.Kind.SEMANTIC_ANALYSIS,
        evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
        text="remote-supplement-marker",
        processor_name="fixture-remote",
        processor_version="1",
    )

    followup_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.MANUAL,
        scope=ProcessingRun.Scope.REMOTE,
        source_fingerprint=base_run.source_fingerprint,
        source_size=base_run.source_size,
        processor_version="1",
        status=ProcessingRun.Status.SUCCEEDED,
        configuration={
            "workflow": "cloudflare-followup-consented-v1",
            "aggregate_run_id": str(remote_run.pk),
        },
    )
    followup_stage = make_stage(
        followup_run,
        kind=ProcessingStage.Kind.REMOTE_SEMANTIC,
    )
    ProcessingResult.objects.create(
        stage=followup_stage,
        result_kind=ProcessingResult.Kind.SEMANTIC_ANALYSIS,
        evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
        text="followup-supplement-marker",
        processor_name="fixture-followup",
        processor_version="1",
    )

    rematch_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.MANUAL,
        scope=ProcessingRun.Scope.LOCAL,
        source_fingerprint=base_run.source_fingerprint,
        source_size=base_run.source_size,
        processor_version="1",
        status=ProcessingRun.Status.SUCCEEDED,
        configuration={
            "workflow": "identity_gallery_rematch",
            "modality": "writing",
        },
    )
    rematch_stage = make_stage(
        rematch_run,
        kind=ProcessingStage.Kind.WRITING_IDENTITY,
    )
    ProcessingResult.objects.create(
        stage=rematch_stage,
        result_kind=ProcessingResult.Kind.AUTHORSHIP_CANDIDATES,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        text="rematch-supplement-marker",
        processor_name="fixture-rematch",
        processor_version="1",
    )

    response = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    assert response.context["selected_run"] == base_run
    content = response.content.decode()
    assert "base-deterministic-marker" in content
    assert "remote-supplement-marker" in content
    assert "followup-supplement-marker" in content
    assert "rematch-supplement-marker" in content

    explicit_remote = client.get(
        reverse("catalogue-item-detail", args=(item.pk,)),
        {"run": remote_run.pk},
    )
    explicit_content = explicit_remote.content.decode()
    assert explicit_remote.context["selected_run"] == remote_run
    assert explicit_remote.context["selected_run_is_historical"] is False
    assert "remote-supplement-marker" in explicit_content
    assert "base-deterministic-marker" not in explicit_content
    assert "followup-supplement-marker" not in explicit_content
    assert "rematch-supplement-marker" not in explicit_content


def test_default_evidence_requires_live_body_but_explicit_history_remains(client):
    item = make_message_item("Original canonical body")
    run = make_run(item)
    stage = make_stage(run)
    ProcessingResult.objects.create(
        stage=stage,
        result_kind=ProcessingResult.Kind.TEXT_EXTRACTION,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        text="stale-result-marker",
        processor_name="fixture",
        processor_version="1",
    )

    Message.objects.filter(pk=item.pk).update(body="Out-of-band replacement")

    current = client.get(reverse("catalogue-item-detail", args=(item.pk,)))
    assert current.status_code == 200
    assert current.context["selected_run"] is None
    assert b"stale-result-marker" not in current.content

    historical = client.get(
        reverse("catalogue-item-detail", args=(item.pk,)),
        {"run": run.pk},
    )
    assert historical.status_code == 200
    assert historical.context["selected_run_is_historical"] is True
    assert b"stale-result-marker" in historical.content
    assert b"Historical run" in historical.content


def test_default_evidence_requires_live_media_bytes(tmp_path, client, settings):
    settings.MEDIA_ROOT = tmp_path / "media"
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    image = Image.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("source.bin", b"original-media-bytes"),
    )
    run = create_local_run(item, enqueue=False)
    stage = run.stages.get(kind=ProcessingStage.Kind.IMAGE_METADATA)
    ProcessingStage.objects.filter(pk=stage.pk).update(
        status=ProcessingStage.Status.SUCCEEDED
    )
    ProcessingResult.objects.create(
        stage=stage,
        result_kind=ProcessingResult.Kind.IMAGE_METADATA,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        text="stale-media-marker",
        processor_name="fixture",
        processor_version="1",
    )

    Path(image.file.path).write_bytes(b"replacement-media-bytes")

    current = client.get(reverse("catalogue-item-detail", args=(item.pk,)))
    assert current.status_code == 200
    assert current.context["selected_run"] is None
    assert b"stale-media-marker" not in current.content

    historical = client.get(
        reverse("catalogue-item-detail", args=(item.pk,)),
        {"run": run.pk},
    )
    assert historical.context["selected_run_is_historical"] is True
    assert b"stale-media-marker" in historical.content


def test_blank_media_caption_supersedes_unfinished_local_and_remote_runs(client):
    media_item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    image = Image.objects.create(catalog_item=media_item)
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE_MESSAGE)
    message = Message.objects.create(catalog_item=item, body="Caption to remove")
    ImageMessage.objects.create(message=message, image=image)
    local_run = create_local_run(item, enqueue=False)
    current_source = source_fingerprint(item.pk)
    remote_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.MANUAL,
        scope=ProcessingRun.Scope.REMOTE,
        source_fingerprint=current_source.sha256,
        source_size=current_source.size_bytes,
        processor_version="1",
        status=ProcessingRun.Status.RUNNING,
    )
    remote_stage = ProcessingStage.objects.create(
        run=remote_run,
        kind=ProcessingStage.Kind.REMOTE_SEMANTIC,
        status=ProcessingStage.Status.QUEUED,
        processor_name="fixture-remote",
    )
    form = MessageMetadataForm(
        {
            "title": "",
            "occurred_at": "",
            "body": "   ",
            "sender_name": "",
            "sender_identifier": "",
            "sender_type": "",
            "recipients": "",
            "reply_to": "",
        },
        item=item,
    )

    assert form.is_valid(), form.errors
    form.save()

    local_run.refresh_from_db()
    remote_run.refresh_from_db()
    remote_stage.refresh_from_db()
    message.refresh_from_db()
    assert message.body == ""
    assert local_run.status == ProcessingRun.Status.SUPERSEDED
    assert remote_run.status == ProcessingRun.Status.SUPERSEDED
    assert remote_stage.status == ProcessingStage.Status.SUPERSEDED
    assert ProcessingRun.objects.filter(catalog_item=item).count() == 2
    detail = client.get(reverse("catalogue-item-detail", args=(item.pk,)))
    assert detail.status_code == 200
    assert detail.context["selected_run"] is None


def test_catalogue_card_links_to_item_detail(client):
    item = make_message_item()

    response = client.get(reverse("catalogue"))

    assert (
        reverse("catalogue-item-detail", args=(item.pk,)) in response.content.decode()
    )


def test_status_fragment_polls_only_while_run_is_active(client):
    item = make_message_item()
    run = make_run(item, status=ProcessingRun.Status.RUNNING)
    make_stage(run, status=ProcessingStage.Status.RUNNING)

    active = client.get(
        reverse("catalogue-item-processing-status", args=(item.pk,)),
        {"run": run.pk},
    )
    assert active.status_code == 200
    assert 'hx-trigger="every 2s"' in active.content.decode()

    ProcessingRun.objects.filter(pk=run.pk).update(status=ProcessingRun.Status.FAILED)
    terminal = client.get(
        reverse("catalogue-item-processing-status", args=(item.pk,)),
        {"run": run.pk},
    )
    assert 'hx-trigger="every 2s"' not in terminal.content.decode()


def test_manual_rerun_and_failed_retry_create_fresh_versioned_runs(client):
    item = make_message_item()
    old_run = make_run(item, status=ProcessingRun.Status.FAILED)
    failed = make_stage(old_run, status=ProcessingStage.Status.FAILED)
    new_run = SimpleNamespace(pk=uuid.uuid4())

    with patch(
        "pandect.processing.views.create_local_run", return_value=new_run
    ) as create:
        rerun = client.post(reverse("catalogue-item-rerun", args=(item.pk,)))
        assert rerun.status_code == 302
        assert "notice=rerun_started" in rerun["Location"]
        create.assert_called_once_with(item, trigger=ProcessingRun.Trigger.MANUAL)

    retry_run = SimpleNamespace(pk=uuid.uuid4())
    with patch(
        "pandect.processing.views.create_local_run", return_value=retry_run
    ) as create:
        retry = client.post(
            reverse("catalogue-item-retry-stage", args=(item.pk, failed.pk))
        )
        assert retry.status_code == 302
        assert "notice=retry_started" in retry["Location"]
        configuration = create.call_args.kwargs["configuration"]
        assert configuration["manual_retry"]["stage_id"] == str(failed.pk)
        assert (
            ProcessingStage.objects.get(pk=failed.pk).status
            == ProcessingStage.Status.FAILED
        )


def test_aware_timestamp_promotion_is_audited_and_updates_only_occurred_at(client):
    item = make_message_item()
    original_title = item.title
    run = make_run(item)
    stage = make_stage(run)
    claimed = datetime(2026, 8, 12, 15, 45, tzinfo=UTC)
    claim = TimestampClaim.objects.create(
        stage=stage,
        catalog_item=item,
        namespace="EXIF",
        tag="DateTimeOriginal",
        raw_value="2026:08:12 15:45:00Z",
        precision=TimestampClaim.Precision.SECOND,
        parsed_value=claimed,
        timezone_evidence="explicit_offset",
    )

    response = client.post(
        reverse("catalogue-item-promote-timestamp", args=(item.pk, claim.pk)),
        {"timezone_name": "", "fold": "", "operator_note": "Selected capture time"},
    )

    assert response.status_code == 302
    item.refresh_from_db()
    assert item.occurred_at == claimed
    assert item.title == original_title
    decision = TimestampDecision.objects.get()
    assert decision.claim == claim
    assert decision.previous_occurred_at is None
    assert decision.operator_note == "Selected capture time"


def test_naive_ambiguous_timestamp_requires_timezone_and_fold(client):
    item = make_message_item()
    run = make_run(item)
    stage = make_stage(run)
    claim = TimestampClaim.objects.create(
        stage=stage,
        catalog_item=item,
        namespace="EXIF",
        tag="DateTimeOriginal",
        raw_value="2025:11:02 01:30:00",
        precision=TimestampClaim.Precision.SECOND,
        timezone_evidence="naive",
    )
    url = reverse("catalogue-item-promote-timestamp", args=(item.pk, claim.pk))

    missing_fold = client.post(
        url,
        {"timezone_name": "America/Toronto", "fold": "", "operator_note": ""},
    )
    assert missing_fold.status_code == 400
    assert "ambiguous local time" in missing_fold.content.decode()
    assert TimestampDecision.objects.count() == 0

    promoted = client.post(
        url,
        {"timezone_name": "America/Toronto", "fold": "1", "operator_note": ""},
    )
    assert promoted.status_code == 302
    item.refresh_from_db()
    assert item.occurred_at == datetime(2025, 11, 2, 6, 30, tzinfo=UTC)
    decision = TimestampDecision.objects.get()
    assert decision.timezone_name == "America/Toronto"
    assert decision.fold == 1


def test_timestamp_promotion_rolls_back_when_it_would_invalidate_an_event(client):
    original = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    item = CatalogItem.objects.create(
        item_type=CatalogItem.ItemType.EVENT,
        occurred_at=original,
    )
    Event.objects.create(
        catalog_item=item,
        ended_at=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    )
    run = make_run(item)
    stage = make_stage(run)
    claim = TimestampClaim.objects.create(
        stage=stage,
        catalog_item=item,
        namespace="metadata",
        tag="CreateDate",
        raw_value="2026-08-12 13:00:00Z",
        precision=TimestampClaim.Precision.SECOND,
        parsed_value=datetime(2026, 8, 12, 13, 0, tzinfo=UTC),
        timezone_evidence="explicit_offset",
    )

    response = client.post(
        reverse("catalogue-item-promote-timestamp", args=(item.pk, claim.pk)),
        {"timezone_name": "", "fold": "", "operator_note": ""},
    )

    assert response.status_code == 400
    item.refresh_from_db()
    assert item.occurred_at == original
    assert not TimestampDecision.objects.exists()


def test_subject_reference_and_candidate_decisions_do_not_rewrite_sender(client):
    sender = CommunicationParticipant.objects.create(display_name="Message sender")
    item = make_message_item(sender=sender)
    observation = make_writing_observation(item)
    subject = Subject.objects.create(label="Local subject")

    reference_response = client.post(
        reverse("catalogue-item-add-reference", args=(item.pk, observation.pk)),
        {"subject": subject.pk, "notes": "Operator selected"},
    )
    assert reference_response.status_code == 302
    reference = SubjectReference.objects.get()
    assert reference.subject == subject

    run = make_run(item)
    stage = make_stage(run, kind=ProcessingStage.Kind.WRITING_IDENTITY)
    gallery_fingerprint = rank_closed_gallery(
        observation_vector(observation),
        [reference_vector(reference)],
    ).gallery_fingerprint

    candidate = IdentityCandidate.objects.create(
        candidate_set_id=uuid.uuid4(),
        producing_run=run,
        producing_stage=stage,
        observation=observation,
        subject=subject,
        modality=observation.modality,
        feature_space=observation.feature_space,
        gallery_fingerprint=gallery_fingerprint,
        rank=1,
        similarity_score=0.72,
        reference_count=3,
        calibration_revision="uncalibrated-v1",
    )
    decision_response = client.post(
        reverse("catalogue-item-decide-candidate", args=(item.pk, candidate.pk)),
        {"decision": IdentityCandidate.Decision.CONFIRMED, "note": "Confirmed locally"},
    )

    assert decision_response.status_code == 302
    assertion = IdentityAssertion.objects.get()
    assert assertion.subject == subject
    item.message.refresh_from_db()
    assert item.message.sender == sender


def test_item_detail_hides_stale_observation_and_rejects_its_candidate_set(client):
    item = make_message_item()
    observation = make_writing_observation(item)
    subject = Subject.objects.create(label="Stale candidate")
    old_run = make_run(item)
    old_stage = make_stage(old_run, kind=ProcessingStage.Kind.WRITING_IDENTITY)
    fingerprint = rank_closed_gallery(
        observation_vector(observation), []
    ).gallery_fingerprint
    candidate = IdentityCandidate.objects.create(
        candidate_set_id=uuid.uuid4(),
        producing_run=old_run,
        producing_stage=old_stage,
        observation=observation,
        subject=subject,
        modality=observation.modality,
        feature_space=observation.feature_space,
        gallery_fingerprint=fingerprint,
        rank=1,
        similarity_score=0.72,
        reference_count=3,
        calibration_revision="uncalibrated-v1",
    )
    Message.objects.filter(pk=item.pk).update(body="Out-of-band new body")

    response = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    content = response.content.decode()
    assert response.status_code == 200
    assert str(observation.pk)[:8] not in content
    assert "Cosine similarity 0.7200" not in content

    decision_response = client.post(
        reverse("catalogue-item-decide-candidate", args=(item.pk, candidate.pk)),
        {"decision": IdentityCandidate.Decision.CONFIRMED, "note": "Too late"},
    )
    assert decision_response.status_code == 400
    candidate.refresh_from_db()
    assert candidate.decision == IdentityCandidate.Decision.SUGGESTED
    assert not IdentityCandidateDecision.objects.exists()


@pytest.mark.parametrize(
    "modality",
    (
        IdentityObservation.Modality.FACE,
        IdentityObservation.Modality.VOICE,
        IdentityObservation.Modality.WRITING,
    ),
)
def test_replaced_source_preserves_but_excludes_identity_references(
    modality,
    tmp_path,
    client,
    settings,
):
    settings.MEDIA_ROOT = tmp_path / "media"
    if modality == IdentityObservation.Modality.FACE:
        item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
        extension = Image.objects.create(
            catalog_item=item,
            file=SimpleUploadedFile("face.bin", b"original-media"),
        )
        location = {"bbox": [1, 2, 30, 40]}
        sample_fields = {}
    elif modality == IdentityObservation.Modality.VOICE:
        item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.AUDIO)
        extension = Audio.objects.create(
            catalog_item=item,
            file=SimpleUploadedFile("voice.bin", b"original-media"),
        )
        location = {"start_ms": 0, "end_ms": 10_000, "stream_index": 0}
        sample_fields = {"sample_duration_ms": 10_000}
    else:
        item = make_message_item("AAAAAAAAAAAA")
        extension = None
        location = {"start": 0, "end": 12}
        sample_fields = {"sample_word_count": 4}

    origin_run = make_run(item)
    observations = [
        IdentityObservation.objects.create(
            catalog_item=item,
            modality=modality,
            unit_key=f"{modality}:{index}",
            source_location=location,
            source_sha256="d" * 64,
            embedding=encode_embedding((1.0, float(index))),
            embedding_dimension=2,
            feature_space=f"{modality}-replacement-test-v1",
            processor_name="fixture",
            processor_version="1",
            preprocessing_version="1",
            provenance={"processing_run_id": str(origin_run.pk)},
            quality_status=IdentityObservation.QualityStatus.APPROVED,
            quality_details={"fixture": True},
            **sample_fields,
        )
        for index in range(2)
    ]
    original_subject = Subject.objects.create(label="Historical subject")
    replacement_subject = Subject.objects.create(label="Replacement subject")
    reference = SubjectReference.objects.create(
        subject=original_subject,
        observation=observations[0],
    )
    assert len(current_reference_vectors((reference,))) == 1

    fresh_detail = client.get(reverse("catalogue-item-detail", args=(item.pk,)))
    assert str(observations[0].pk)[:8] in fresh_detail.content.decode()
    assert str(observations[1].pk)[:8] in fresh_detail.content.decode()

    if extension is None:
        Message.objects.filter(pk=item.pk).update(body="BBBBBBBBBBBB")
    else:
        Path(extension.file.path).write_bytes(b"changed-media!")

    stale_detail = client.get(reverse("catalogue-item-detail", args=(item.pk,)))
    content = stale_detail.content.decode()
    assert stale_detail.status_code == 200
    assert str(observations[0].pk)[:8] not in content
    assert str(observations[1].pk)[:8] not in content

    rejected_add = client.post(
        reverse(
            "catalogue-item-add-reference",
            args=(item.pk, observations[1].pk),
        ),
        {"subject": replacement_subject.pk, "notes": "stale add"},
    )
    assert rejected_add.status_code == 400
    assert b"Historical observations cannot become references" in rejected_add.content
    assert not SubjectReference.objects.filter(observation=observations[1]).exists()

    rejected_update = client.post(
        reverse(
            "catalogue-item-update-reference",
            args=(item.pk, observations[0].pk),
        ),
        {"subject": replacement_subject.pk, "notes": "stale update"},
    )
    assert rejected_update.status_code == 400
    reference.refresh_from_db()
    assert reference.subject == original_subject
    assert current_reference_vectors((reference,)) == ()


def test_item_detail_shows_only_the_newest_compatible_candidate_set(client):
    item = make_message_item()
    observation = make_writing_observation(item)
    older_subject = Subject.objects.create(label="Older candidate")
    newer_subject = Subject.objects.create(label="Newer candidate")
    fingerprint = rank_closed_gallery(
        observation_vector(observation), []
    ).gallery_fingerprint
    older_run = make_run(item)
    older_stage = make_stage(
        older_run,
        kind=ProcessingStage.Kind.WRITING_IDENTITY,
    )
    IdentityCandidate.objects.create(
        candidate_set_id=uuid.uuid4(),
        producing_run=older_run,
        producing_stage=older_stage,
        observation=observation,
        subject=older_subject,
        modality=observation.modality,
        feature_space=observation.feature_space,
        gallery_fingerprint=fingerprint,
        rank=1,
        similarity_score=0.72,
        reference_count=3,
        calibration_revision="uncalibrated-v1",
    )
    newer_run = make_run(item)
    newer_stage = make_stage(
        newer_run,
        kind=ProcessingStage.Kind.WRITING_IDENTITY,
    )
    IdentityCandidate.objects.create(
        candidate_set_id=uuid.uuid4(),
        producing_run=newer_run,
        producing_stage=newer_stage,
        observation=observation,
        subject=newer_subject,
        modality=observation.modality,
        feature_space=observation.feature_space,
        gallery_fingerprint=fingerprint,
        rank=1,
        similarity_score=0.81,
        reference_count=3,
        calibration_revision="uncalibrated-v1",
    )

    response = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    content = response.content.decode()
    assert response.status_code == 200
    assert "Cosine similarity 0.8100" in content
    assert "Cosine similarity 0.7200" not in content


def test_reference_changes_schedule_bounded_rematches_only_after_commit(
    client,
    django_capture_on_commit_callbacks,
):
    sender = CommunicationParticipant.objects.create(display_name="Message sender")
    item = make_message_item(sender=sender)
    observation = make_writing_observation(item)
    first = Subject.objects.create(label="First local subject")
    second = Subject.objects.create(label="Second local subject")

    with patch(
        "pandect.processing.identity_rematch._dispatch_gallery_rematch_safely"
    ) as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                reverse(
                    "catalogue-item-add-reference",
                    args=(item.pk, observation.pk),
                ),
                {"subject": first.pk, "notes": "Initial selection"},
            )
        assert response.status_code == 302
        selected = SubjectReferenceDecision.objects.get()
        selected_job = IdentityGalleryRematchJob.objects.get(decision=selected)
        assert selected.action == SubjectReferenceDecision.Action.SELECTED
        assert selected.subject == first
        assert selected.subject_label == first.label
        assert selected.notes == "Initial selection"
        dispatch.assert_called_once_with(str(selected_job.pk))

    with patch(
        "pandect.processing.identity_rematch._dispatch_gallery_rematch_safely"
    ) as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                reverse(
                    "catalogue-item-update-reference",
                    args=(item.pk, observation.pk),
                ),
                {"subject": second.pk, "notes": "Changed selection"},
            )
        assert response.status_code == 302
        updated = SubjectReferenceDecision.objects.exclude(pk=selected.pk).get()
        updated_job = IdentityGalleryRematchJob.objects.get(decision=updated)
        assert updated.action == SubjectReferenceDecision.Action.UPDATED
        assert updated.previous_subject == first
        assert updated.previous_subject_label == first.label
        assert updated.subject == second
        assert updated.subject_label == second.label
        assert updated.previous_notes == "Initial selection"
        assert updated.notes == "Changed selection"
        dispatch.assert_called_once_with(str(updated_job.pk))
    reference = SubjectReference.objects.get(observation=observation)
    assert reference.subject == second
    assert reference.notes == "Changed selection"

    with patch(
        "pandect.processing.identity_rematch._dispatch_gallery_rematch_safely"
    ) as dispatch:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                reverse(
                    "catalogue-item-remove-reference",
                    args=(item.pk, observation.pk),
                )
            )
        assert response.status_code == 302
        removed = SubjectReferenceDecision.objects.exclude(
            pk__in=(selected.pk, updated.pk)
        ).get()
        removed_job = IdentityGalleryRematchJob.objects.get(decision=removed)
        assert removed.action == SubjectReferenceDecision.Action.REMOVED
        assert removed.subject == second
        assert removed.subject_label == second.label
        assert removed.notes == "Changed selection"
        dispatch.assert_called_once_with(str(removed_job.pk))

    assert list(
        SubjectReferenceDecision.objects.order_by(
            "decided_at", "created_at"
        ).values_list("action", flat=True)
    ) == ["selected", "updated", "removed"]

    assert not SubjectReference.objects.filter(observation=observation).exists()
    item.message.refresh_from_db()
    assert item.message.sender == sender

    detail = client.get(reverse("catalogue-item-detail", args=(item.pk,)))
    assert detail.status_code == 200
    assert b"Reference decision history (3)" in detail.content
    assert b"Initial selection" in detail.content
    assert b"Changed selection" in detail.content

    selected.notes = "attempted rewrite"
    with pytest.raises(TypeError, match="append-only"):
        selected.save()
    with pytest.raises(TypeError, match="append-only"):
        selected.delete()
    with pytest.raises(TypeError, match="append-only"):
        SubjectReferenceDecision.objects.filter(pk=selected.pk).update(notes="rewrite")
    with pytest.raises(TypeError, match="append-only"):
        SubjectReferenceDecision.objects.filter(pk=selected.pk).delete()


def test_subject_creation_is_item_scoped_navigation_but_creates_local_identity(client):
    item = make_message_item()

    response = client.post(
        reverse("catalogue-item-create-subject", args=(item.pk,)),
        {"label": "  Locally known person  ", "notes": "Operator context"},
    )

    assert response.status_code == 302
    assert "notice=subject_created" in response["Location"]
    subject = Subject.objects.get()
    assert subject.label == "Locally known person"
    assert subject.notes == "Operator context"


def test_unapproved_observation_cannot_become_a_reference(client):
    item = make_message_item()
    observation = make_writing_observation(
        item,
        quality=IdentityObservation.QualityStatus.UNASSESSED,
    )
    subject = Subject.objects.create(label="Subject")

    response = client.post(
        reverse("catalogue-item-add-reference", args=(item.pk, observation.pk)),
        {"subject": subject.pk, "notes": ""},
    )

    assert response.status_code == 400
    assert not SubjectReference.objects.exists()
    assert "quality-approved" in response.content.decode()


@override_settings(CLOUDFLARE_AI_ENABLED=True)
def test_remote_plan_is_visible_but_requires_explicit_acknowledgement(client):
    item = make_message_item()
    manifest = ConsentManifest(
        {
            "version": "1",
            "notice_version": REMOTE_NOTICE_VERSION,
            "adapter_privacy_contract": {"version": "test-adapter-v1"},
        },
        "a" * 64,
    )
    plan = RemotePlan(
        item_id=item.pk,
        item_type=item.item_type,
        source_sha256="b" * 64,
        source_size=4,
        operations=("semantic",),
        artifact_ids=(),
        payloads=(
            {
                "transfer": "exact_message_body",
                "byte_size": 4,
                "sha256": "b" * 64,
            },
        ),
        model_ids={
            "semantic": "@cf/meta/llama-3.2-11b-vision-instruct",
        },
        prompt_versions={"semantic": "semantic-schema-v1"},
        prompt_hashes={"semantic": "c" * 64},
        recipients=("Cloudflare AI Gateway",),
        destination={
            "cloudflare_account_id": "account",
            "cloudflare_gateway_id": "pandect",
            "adapter_policy": "cloudflare-direct-http-v1",
        },
        manifest=manifest,
    )
    with patch("pandect.processing.views.build_remote_plan", return_value=plan):
        response = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    assert response.status_code == 200
    assert b"exact_message_body" in response.content
    assert manifest.sha256.encode() in response.content
    assert (
        f'name="notice_version" value="{REMOTE_NOTICE_VERSION}"'
        in response.content.decode()
    )

    response = client.post(
        reverse("catalogue-item-remote-consent", args=(item.pk,)),
        {"manifest_hash": manifest.sha256},
    )
    assert response.status_code == 400


@override_settings(CLOUDFLARE_AI_ENABLED=True)
def test_exact_original_image_consent_explicitly_discloses_embedded_metadata(client):
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    Image.objects.create(
        catalog_item=item,
        file=SimpleUploadedFile("source.jpg", b"exact-image"),
        mime_type="image/jpeg",
        original_filename="source.jpg",
    )
    manifest = ConsentManifest(
        {
            "version": "1",
            "notice_version": REMOTE_NOTICE_VERSION,
            "adapter_privacy_contract": {"version": "test-adapter-v1"},
        },
        "a" * 64,
    )
    plan = RemotePlan(
        item_id=item.pk,
        item_type=item.item_type,
        source_sha256="b" * 64,
        source_size=11,
        operations=("image_description",),
        artifact_ids=(),
        payloads=(
            {
                "transfer": "exact_original_image",
                "embedded_metadata_included": True,
                "byte_size": 11,
                "sha256": "b" * 64,
            },
        ),
        model_ids={"image_description": "@cf/meta/llama-3.2-11b-vision-instruct"},
        prompt_versions={"image_description": "vision-schema-v1"},
        prompt_hashes={"image_description": "c" * 64},
        recipients=("Cloudflare AI Gateway",),
        destination={
            "cloudflare_account_id": "account",
            "cloudflare_gateway_id": "pandect",
            "adapter_policy": "cloudflare-direct-http-v1",
        },
        manifest=manifest,
    )
    with patch("pandect.processing.views.build_remote_plan", return_value=plan):
        response = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    content = response.content.decode()
    assert response.status_code == 200
    assert "exact original image bytes include all embedded" in content
    assert "EXIF, XMP, IPTC, and GPS metadata" in content


@override_settings(CLOUDFLARE_AI_ENABLED=True)
def test_remote_consent_action_passes_exact_hash_to_new_run(client):
    item = make_message_item()
    remote_run = make_run(item)
    with patch(
        "pandect.processing.views.create_consented_remote_run",
        return_value=remote_run,
    ) as create:
        response = client.post(
            reverse("catalogue-item-remote-consent", args=(item.pk,)),
            {
                "manifest_hash": "c" * 64,
                "notice_version": REMOTE_NOTICE_VERSION,
                "acknowledge_transfer": "on",
            },
        )

    assert response.status_code == 302
    create.assert_called_once_with(
        item,
        expected_manifest_hash="c" * 64,
        notice_version=REMOTE_NOTICE_VERSION,
    )


@override_settings(CLOUDFLARE_AI_ENABLED=True)
def test_followup_plan_is_shown_as_a_separate_explicit_consent(client):
    item = make_message_item()
    manifest = ConsentManifest(
        {
            "version": "1",
            "notice_version": FOLLOWUP_NOTICE_VERSION,
            "workflow": "cloudflare-followup-consented-v1",
            "adapter_privacy_contract": {"version": "test-adapter-v1"},
        },
        "f" * 64,
    )
    plan = FollowupPlan(
        item_id=item.pk,
        item_type=CatalogItem.ItemType.AUDIO,
        source_sha256="b" * 64,
        source_size=15,
        aggregate_run_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        operations=("transcript_semantic",),
        payloads=(
            {
                "transfer": "exact_structured_merged_transcript",
                "byte_size": 15,
                "sha256": "d" * 64,
                "input_results": [
                    {
                        "result_id": "00000000-0000-0000-0000-000000000002",
                        "result_kind": "transcript",
                        "result_sha256": "e" * 64,
                    }
                ],
            },
        ),
        model_ids={"transcript_semantic": "@cf/meta/llama-3.2-11b-vision-instruct"},
        prompt_versions={"transcript_semantic": "structured-transcript-analysis-v2"},
        prompt_hashes={"transcript_semantic": "a" * 64},
        recipients=("Cloudflare AI Gateway",),
        destination={
            "cloudflare_account_id": "account",
            "cloudflare_gateway_id": "pandect",
            "adapter_policy": "cloudflare-direct-http-v1",
        },
        input_result_hashes={"00000000-0000-0000-0000-000000000002": "e" * 64},
        manifest=manifest,
        _contents=(("transcript_semantic", b"Meet at noon."),),
    )
    with (
        patch("pandect.processing.views.build_remote_plan") as initial,
        patch(
            "pandect.processing.views.build_followup_plan",
            return_value=plan,
        ),
    ):
        initial.side_effect = RemotePlanError("Initial plan unavailable.")
        response = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    content = response.content.decode()
    assert response.status_code == 200
    assert "separate consent" in content
    assert "exact_structured_merged_transcript" in content
    assert manifest.sha256 in content
    assert f'name="notice_version" value="{FOLLOWUP_NOTICE_VERSION}"' in content
    assert reverse("catalogue-item-remote-followup-consent", args=(item.pk,)) in content

    response = client.post(
        reverse("catalogue-item-remote-followup-consent", args=(item.pk,)),
        {"manifest_hash": manifest.sha256},
    )
    assert response.status_code == 400


@override_settings(CLOUDFLARE_AI_ENABLED=True)
def test_followup_consent_action_passes_exact_hash_to_a_new_run(client):
    item = make_message_item()
    remote_run = make_run(item)
    with patch(
        "pandect.processing.views.create_consented_followup_run",
        return_value=remote_run,
    ) as create:
        response = client.post(
            reverse("catalogue-item-remote-followup-consent", args=(item.pk,)),
            {
                "manifest_hash": "f" * 64,
                "notice_version": FOLLOWUP_NOTICE_VERSION,
                "acknowledge_transfer": "on",
            },
        )

    assert response.status_code == 302
    assert "notice=remote_followup_started" in response["Location"]
    create.assert_called_once_with(
        item,
        expected_manifest_hash="f" * 64,
        notice_version=FOLLOWUP_NOTICE_VERSION,
    )

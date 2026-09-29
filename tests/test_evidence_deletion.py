"""Parent deletion must not bypass append-only model deletion guards."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from django.db.models import PROTECT
from django.db.models.deletion import Collector, ProtectedError

from pandect.catalog.models import CatalogItem
from pandect.identities.models import IdentityObservation
from pandect.identities.services import encode_embedding
from pandect.photos.models import Image
from pandect.processing.models import (
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
    RemoteConsent,
    TimestampClaim,
    TimestampDecision,
)

PROTECTED_RELATIONSHIPS = [
    (ProcessingResult, "stage"),
    (DerivedArtifact, "stage"),
    (DerivedArtifact, "catalog_item"),
    (TimestampClaim, "stage"),
    (TimestampClaim, "catalog_item"),
    (TimestampDecision, "claim"),
    (TimestampDecision, "catalog_item"),
    (RemoteConsent, "run"),
    (RemoteConsent, "catalog_item"),
    (IdentityObservation, "catalog_item"),
]


@pytest.mark.parametrize(("model", "field"), PROTECTED_RELATIONSHIPS)
def test_evidence_foreign_keys_protect_their_parent(model, field):
    # In addition to behavioral tests, guard every relation independently:
    # another protected relation must not mask a new cascade on this field.
    assert model._meta.get_field(field).remote_field.on_delete is PROTECT


@pytest.fixture
def evidence_source(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.IMAGE)
    original = b"Synthetic original; no real investigation content."
    image = Image.objects.create(
        catalog_item=item,
        file=ContentFile(original, name="original.bin"),
    )
    run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.MANUAL,
        source_fingerprint=hashlib.sha256(original).hexdigest(),
        source_size=len(original),
    )
    stage = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.IMAGE_METADATA,
    )
    return {"item": item, "run": run, "stage": stage, "image": image}


def create_evidence(kind, source):
    item, run, stage = (source[key] for key in ("item", "run", "stage"))
    if kind == "result":
        return ProcessingResult.objects.create(
            stage=stage,
            result_kind=ProcessingResult.Kind.IMAGE_METADATA,
            evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
            data={"synthetic": True},
            processor_name="test-metadata",
            processor_version="1",
        )
    if kind == "artifact":
        content = b"Synthetic derivative."
        return DerivedArtifact.objects.create(
            stage=stage,
            catalog_item=item,
            file=ContentFile(content, name="derivative.txt"),
            role=DerivedArtifact.Role.OTHER,
            mime_type="text/plain",
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
        )
    if kind in {"claim", "decision"}:
        claim = TimestampClaim.objects.create(
            stage=stage,
            catalog_item=item,
            namespace="synthetic",
            tag="timestamp",
            raw_value="2026-09-24T10:00:00Z",
            parsed_value=datetime(2026, 9, 24, 10, tzinfo=UTC),
        )
        if kind == "claim":
            return claim
        return TimestampDecision.objects.create(
            claim=claim,
            catalog_item=item,
            resulting_occurred_at=claim.parsed_value,
        )
    if kind == "consent":
        return RemoteConsent.objects.create(
            run=run,
            catalog_item=item,
            notice_version="test-1",
            source_fingerprint=run.source_fingerprint,
            payload_manifest={"synthetic": True},
            payload_manifest_hash="c" * 64,
            recipients=["Synthetic provider"],
            model_ids={"image": "test-model"},
            planned_request_count=1,
        )
    if kind == "observation":
        return IdentityObservation.objects.create(
            catalog_item=item,
            modality=IdentityObservation.Modality.FACE,
            unit_key="face:0",
            source_location={"bbox": [1, 2, 3, 4]},
            source_sha256=run.source_fingerprint,
            embedding=encode_embedding((1.0, 0.0)),
            embedding_dimension=2,
            feature_space="test-space",
            processor_name="test-observation",
            processor_version="1",
            preprocessing_version="1",
            provenance={"synthetic": True},
            quality_details={"synthetic": True},
        )
    raise AssertionError(f"Unknown evidence fixture: {kind}")


PARENT_CASES = [
    (kind, parent)
    for kind in ("result", "artifact", "claim", "decision", "consent", "observation")
    for parent in (
        ("item",)
        if kind == "observation"
        else ("item", "run")
        if kind == "consent"
        else ("item", "run", "stage")
    )
]


@pytest.mark.django_db
@pytest.mark.parametrize("bulk", [False, True], ids=["instance", "queryset"])
@pytest.mark.parametrize(("kind", "parent"), PARENT_CASES)
def test_parent_deletion_preserves_evidence_and_files(
    evidence_source, kind, parent, bulk
):
    evidence = create_evidence(kind, evidence_source)
    target = evidence_source[parent]
    models = {CatalogItem, Image, ProcessingRun, ProcessingStage, type(evidence)}
    if kind == "decision":
        models.add(TimestampClaim)
    before = {model: list(model.objects.values()) for model in models}
    files = [Path(evidence_source["image"].file.path)]
    if kind == "artifact":
        files.append(Path(evidence.file.path))
    file_contents = {path: path.read_bytes() for path in files}

    with pytest.raises(ProtectedError):
        if bulk:
            type(target).objects.filter(pk=target.pk).delete()
        else:
            target.delete()

    assert {model: list(model.objects.values()) for model in models} == before
    assert {path: path.read_bytes() for path in files} == file_contents


@pytest.mark.django_db
def test_cascade_collector_cannot_delete_a_timestamp_decision(evidence_source):
    decision = create_evidence("decision", evidence_source)
    # A collector never calls the TimestampClaim.delete() guard. Test its own
    # protection independently of the claim's item/stage protections.
    with pytest.raises(ProtectedError):
        Collector(using="default").collect([decision.claim])
    assert TimestampDecision.objects.filter(pk=decision.pk).exists()
    assert TimestampClaim.objects.filter(pk=decision.claim_id).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("bulk", [False, True], ids=["instance", "queryset"])
def test_mixed_parent_deletion_does_not_partially_delete_unprocessed_items(
    evidence_source, bulk
):
    create_evidence("result", evidence_source)
    other = CatalogItem.objects.create(item_type=CatalogItem.ItemType.NOTE)
    before = set(CatalogItem.objects.values_list("pk", flat=True))

    with pytest.raises(ProtectedError):
        if bulk:
            CatalogItem.objects.filter(pk__in=before).delete()
        else:
            evidence_source["item"].delete()

    assert set(CatalogItem.objects.values_list("pk", flat=True)) == before
    assert CatalogItem.objects.filter(pk=other.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("bulk", [False, True], ids=["instance", "queryset"])
def test_unprocessed_item_with_empty_workflow_remains_deletable(evidence_source, bulk):
    item = evidence_source["item"]
    item_id = item.pk
    if bulk:
        CatalogItem.objects.filter(pk=item_id).delete()
    else:
        item.delete()

    assert not CatalogItem.objects.filter(pk=item_id).exists()
    assert not Image.objects.filter(pk=item_id).exists()
    assert not ProcessingRun.objects.exists()
    assert not ProcessingStage.objects.exists()

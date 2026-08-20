from datetime import UTC, datetime

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from pandect.catalog.models import CatalogItem
from pandect.messages.models import Message, TextMessage
from pandect.processing.models import (
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
    RemoteConsent,
    TimestampClaim,
    TimestampDecision,
)


def make_text_item(body: str = "Canonical message") -> CatalogItem:
    item = CatalogItem.objects.create(item_type=CatalogItem.ItemType.TEXT_MESSAGE)
    message = Message.objects.create(catalog_item=item, body=body)
    TextMessage.objects.create(message=message)
    return item


def make_run(item: CatalogItem) -> ProcessingRun:
    return ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.MANUAL,
        source_fingerprint="a" * 64,
        source_size=17,
    )


@pytest.mark.django_db
def test_processing_stage_units_are_unique_within_a_run():
    run = make_run(make_text_item())
    ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.TEXT_EXTRACTION,
    )

    with pytest.raises(IntegrityError):
        ProcessingStage.objects.create(
            run=run,
            kind=ProcessingStage.Kind.TEXT_EXTRACTION,
        )


@pytest.mark.django_db
def test_processing_results_are_append_only_and_keep_provenance():
    run = make_run(make_text_item())
    stage = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.TEXT_EXTRACTION,
    )
    result = ProcessingResult.objects.create(
        stage=stage,
        result_kind=ProcessingResult.Kind.MESSAGE_TEXT_PROFILE,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        data={"counts": {"words": 2}},
        processor_name="pandect.text",
        processor_version="1",
        input_hashes={"source": run.source_fingerprint},
        provenance={"normalization": "none"},
    )

    result.data = {"counts": {"words": 999}}
    with pytest.raises(TypeError, match="append-only"):
        result.save()

    stored = ProcessingResult.objects.get(pk=result.pk)
    assert stored.data == {"counts": {"words": 2}}
    assert stored.input_hashes == {"source": "a" * 64}


@pytest.mark.django_db
def test_all_processing_evidence_rejects_instance_and_bulk_mutation():
    item = make_text_item()
    run = make_run(item)
    stage = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.TEXT_EXTRACTION,
    )
    claim = TimestampClaim.objects.create(
        stage=stage,
        catalog_item=item,
        namespace="message",
        tag="candidate",
        raw_value="2026-08-12T10:00:00Z",
        parsed_value=datetime(2026, 8, 12, 10, tzinfo=UTC),
    )
    records = [
        ProcessingResult.objects.create(
            stage=stage,
            result_kind=ProcessingResult.Kind.TEXT_EXTRACTION,
            evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
            processor_name="pandect.text",
            processor_version="1",
        ),
        DerivedArtifact.objects.create(
            stage=stage,
            catalog_item=item,
            file="derived/test.txt",
            role=DerivedArtifact.Role.OTHER,
            mime_type="text/plain",
            sha256="d" * 64,
            byte_size=4,
        ),
        claim,
        TimestampDecision.objects.create(
            claim=claim,
            catalog_item=item,
            resulting_occurred_at=datetime(2026, 8, 12, 10, tzinfo=UTC),
        ),
        RemoteConsent.objects.create(
            run=run,
            catalog_item=item,
            notice_version="1",
            source_fingerprint=run.source_fingerprint,
            payload_manifest={"kind": "message"},
            payload_manifest_hash="e" * 64,
            recipients=["Cloudflare"],
            model_ids={"text": "example/model"},
            planned_request_count=1,
        ),
    ]

    for record in records:
        model = type(record)
        with pytest.raises(TypeError, match="append-only"):
            record.save()
        with pytest.raises(TypeError, match="append-only"):
            record.delete()
        with pytest.raises(TypeError, match="append-only"):
            model.objects.filter(pk=record.pk).update(id=record.pk)
        with pytest.raises(TypeError, match="append-only"):
            model.objects.bulk_update([record], ["id"])
        with pytest.raises(TypeError, match="append-only"):
            model.objects.filter(pk=record.pk).delete()

        assert model.objects.filter(pk=record.pk).exists()


@pytest.mark.django_db
def test_timestamp_claim_rejects_a_different_catalogue_item():
    item = make_text_item("First")
    other_item = make_text_item("Second")
    run = make_run(item)
    stage = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.TEXT_EXTRACTION,
    )
    claim = TimestampClaim(
        stage=stage,
        catalog_item=other_item,
        namespace="message",
        tag="candidate",
        raw_value="2026-08-12T10:00:00Z",
        parsed_value=datetime(2026, 8, 12, 10, tzinfo=UTC),
    )

    with pytest.raises(ValidationError, match="must match its processing run"):
        claim.full_clean()


@pytest.mark.django_db
def test_remote_consent_is_bound_to_run_item_and_fingerprint():
    item = make_text_item()
    run = make_run(item)
    consent = RemoteConsent(
        run=run,
        catalog_item=item,
        notice_version="1",
        source_fingerprint="b" * 64,
        payload_manifest={"kind": "message", "bytes": 17},
        payload_manifest_hash="c" * 64,
        recipients=["Cloudflare", "configured model provider"],
        model_ids={"text": "example/model"},
        planned_request_count=1,
    )

    with pytest.raises(ValidationError, match="must match the run fingerprint"):
        consent.save()

    assert not RemoteConsent.objects.exists()

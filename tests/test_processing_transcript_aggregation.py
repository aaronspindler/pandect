from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from pandect.audio.models import Audio
from pandect.catalog.models import CatalogItem
from pandect.identities.models import (
    IdentityAssertion,
    IdentityCandidate,
    IdentityObservation,
    Subject,
)
from pandect.identities.services import (
    VIDEO_FACE_AGGREGATE_ROLE,
    VIDEO_FACE_FRAME_ROLE,
    decide_identity_candidate,
    encode_embedding,
)
from pandect.processing.extractors.hashing import sha256_bytes
from pandect.processing.models import (
    DerivedArtifact,
    ProcessingResult,
    ProcessingRun,
    ProcessingStage,
)
from pandect.processing.remote.aggregation import (
    finalize_ready_remote_aggregates,
    finalize_remote_aggregate,
    merge_transcript_chunks,
    parse_subtitle_text,
    verify_video_timeline_bindings,
)
from pandect.processing.services import enqueue_stage
from pandect.videos.models import Video


def _transcript_data(words, *, utterances=()):
    text = " ".join(value.get("punctuated_word", value["word"]) for value in words)
    return {
        "channels": [
            {
                "alternatives": [
                    {
                        "transcript": text,
                        "confidence": 0.9,
                        "words": words,
                    }
                ]
            }
        ],
        "utterances": list(utterances),
    }


def _nested_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(_nested_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_nested_keys(child) for child in value))
    return set()


def test_merge_transcript_shifts_times_and_removes_exact_overlap():
    chunks = [
        {
            "chunk_id": "first",
            "stream_index": 0,
            "source_start_seconds": "0",
            "source_end_seconds": "10",
            "data": _transcript_data(
                [
                    {"word": "hello", "start": 8.4, "end": 8.7, "speaker": 0},
                    {"word": "there", "start": 9.1, "end": 9.4, "speaker": 0},
                ]
            ),
        },
        {
            "chunk_id": "second",
            "stream_index": 0,
            "source_start_seconds": "9",
            "source_end_seconds": "18",
            "data": _transcript_data(
                [
                    {"word": "there", "start": 0.1, "end": 0.4, "speaker": 3},
                    {"word": "friend", "start": 0.7, "end": 1.1, "speaker": 3},
                ],
                utterances=[
                    {
                        "start": 0.1,
                        "end": 1.1,
                        "transcript": "there friend",
                        "speaker": 3,
                    }
                ],
            ),
        },
    ]

    merged = merge_transcript_chunks(chunks)

    stream = merged["streams"][0]
    assert stream["text"] == "hello there friend"
    assert [value["start"] for value in stream["words"]] == [8.4, 9.1, 9.7]
    assert stream["utterances"][0]["start"] == 9.1
    assert stream["utterances"][0]["chunk_speaker"] == 3
    assert stream["utterances"][0]["speaker_scope"] == "chunk_local"
    assert stream["deduplicated_overlap_words"] == [
        {
            "chunk_id": "second",
            "word": "there",
            "start": 9.1,
            "end": 9.4,
            "reason": "exact_token_overlap",
        }
    ]
    assert merged["speaker_reconciliation"]["method"] == (
        "not_reconciled_without_confirmed_local_voice_evidence"
    )


def test_merge_transcript_retains_ambiguous_overlap_alternate():
    chunks = [
        {
            "chunk_id": "first",
            "stream_index": 1,
            "source_start_seconds": 0,
            "source_end_seconds": 10,
            "data": _transcript_data(
                [{"word": "cat", "start": 9.0, "end": 9.5, "confidence": 0.7}]
            ),
        },
        {
            "chunk_id": "second",
            "stream_index": 1,
            "source_start_seconds": 9,
            "source_end_seconds": 19,
            "data": _transcript_data(
                [{"word": "cap", "start": 0.0, "end": 0.5, "confidence": 0.6}]
            ),
        },
    ]

    merged = merge_transcript_chunks(chunks)

    assert merged["text"] == "cat"
    assert merged["ambiguous_alternates"] == [
        {
            "start": 9.0,
            "end": 9.5,
            "reason": "ambiguous_overlap",
            "selected": {
                "word": "cat",
                "start": 9.0,
                "end": 9.5,
                "confidence": 0.7,
                "chunk_start": 9.0,
                "chunk_end": 9.5,
                "chunk_id": "first",
            },
            "alternate": {
                "word": "cap",
                "start": 9.0,
                "end": 9.5,
                "confidence": 0.6,
                "chunk_start": 0.0,
                "chunk_end": 0.5,
                "chunk_id": "second",
            },
            "stream_index": 1,
        }
    ]


def test_subtitle_parser_supports_webvtt_and_ass():
    webvtt = parse_subtitle_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\n<v A>Visible &amp; spoken</v>\n",
        filename="captions.vtt",
    )
    ass = parse_subtitle_text(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
        "Dialogue: 0,0:00:03.00,0:00:04.20,Default,,0,0,0,,{\\i1}Next\\Nline\n",
        filename="captions.ass",
    )

    assert webvtt.cues == ({"start": 1.0, "end": 2.5, "text": "Visible & spoken"},)
    assert ass.cues == (
        {
            "start": 3.0,
            "end": 4.2,
            "text": "Next\nline",
            "source_line": 3,
        },
    )


def _media_run(*, video=False):
    item_type = CatalogItem.ItemType.VIDEO if video else CatalogItem.ItemType.AUDIO
    item = CatalogItem.objects.create(item_type=item_type)
    source = b"canonical-media"
    if video:
        Video.objects.create(
            catalog_item=item,
            file=SimpleUploadedFile("source.mp4", source),
            mime_type="video/mp4",
            original_filename="source.mp4",
        )
    else:
        Audio.objects.create(
            catalog_item=item,
            file=SimpleUploadedFile("source.wav", source),
            mime_type="audio/wav",
            original_filename="source.wav",
        )
    local_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.AUTOMATIC,
        scope=ProcessingRun.Scope.LOCAL,
        source_fingerprint=sha256_bytes(source),
        source_size=len(source),
        status=ProcessingRun.Status.SUCCEEDED,
    )
    chunk_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.AUDIO_SPLIT,
        status=ProcessingStage.Status.SUCCEEDED,
    )
    chunks = []
    for index, (start, end) in enumerate(((0, 10), (9, 18))):
        content = f"chunk-{index}".encode()
        chunks.append(
            DerivedArtifact.objects.create(
                stage=chunk_stage,
                catalog_item=item,
                file=SimpleUploadedFile(f"chunk-{index}.flac", content),
                role=DerivedArtifact.Role.AUDIO_CHUNK,
                mime_type="audio/flac",
                sha256=sha256_bytes(content),
                byte_size=len(content),
                stream_index=0,
                source_start_seconds=f"{start}.000000",
                source_end_seconds=f"{end}.000000",
            )
        )
    remote_run = ProcessingRun.objects.create(
        catalog_item=item,
        trigger=ProcessingRun.Trigger.MANUAL,
        scope=ProcessingRun.Scope.REMOTE,
        source_fingerprint=sha256_bytes(source),
        source_size=len(source),
        status=ProcessingRun.Status.SUCCEEDED,
        configuration={"artifact_ids": [str(value.pk) for value in chunks]},
    )
    return item, local_run, remote_run, chunks


def _remote_result(run, artifact, *, index, data, text):
    stage = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.REMOTE_TRANSCRIPTION,
        unit_key=f"artifact:{artifact.pk}",
        priority=(index + 1) * 10,
        status=ProcessingStage.Status.SUCCEEDED,
        attempts=1,
        max_attempts=1,
    )
    return ProcessingResult.objects.create(
        stage=stage,
        result_kind=ProcessingResult.Kind.TRANSCRIPT,
        evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
        text=text,
        data=data,
        processor_name="remote-test",
        processor_version="1",
        input_hashes={"payload": artifact.sha256},
        provenance={
            "artifact_id": str(artifact.pk),
            "source_start_seconds": str(artifact.source_start_seconds),
            "source_end_seconds": str(artifact.source_end_seconds),
            "stream_index": artifact.stream_index,
        },
    )


def _additional_chunk(
    item,
    chunks,
    *,
    stream_index,
    start,
    end,
):
    content = f"chunk-{stream_index}-{start}-{end}".encode()
    return DerivedArtifact.objects.create(
        stage=chunks[0].stage,
        catalog_item=item,
        file=SimpleUploadedFile(
            f"chunk-{stream_index}-{start}-{end}.flac",
            content,
        ),
        role=DerivedArtifact.Role.AUDIO_CHUNK,
        mime_type="audio/flac",
        sha256=sha256_bytes(content),
        byte_size=len(content),
        stream_index=stream_index,
        source_start_seconds=f"{start}.000000",
        source_end_seconds=f"{end}.000000",
    )


def _voice_observation(
    item,
    local_run,
    artifact,
    *,
    index,
    start_ms,
    end_ms,
    values,
    feature_space="voice-test-v1",
    model_version="1",
    preprocessing_version="mono16k-v1",
):
    return IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.VOICE,
        unit_key=(
            f"stream:{artifact.stream_index}/chunk:{index:03d}/"
            f"voice:{start_ms:010d}-{end_ms:010d}"
        ),
        source_location={
            "start_ms": start_ms,
            "end_ms": end_ms,
            "stream_index": artifact.stream_index,
        },
        source_sha256=artifact.sha256,
        embedding=encode_embedding(values),
        embedding_dimension=len(values),
        feature_space=feature_space,
        processor_name="local-voice-test",
        processor_version="1",
        model_identifier="campp-test",
        model_version=model_version,
        model_sha256="1" * 64,
        preprocessing_version=preprocessing_version,
        provenance={
            "processing_run_id": str(local_run.pk),
            "audio_chunk": {
                "stream_index": artifact.stream_index,
                "sha256": artifact.sha256,
            },
        },
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=0.9,
        quality_details={"status": "usable"},
        sample_duration_ms=end_ms - start_ms,
    )


@pytest.mark.django_db
def test_audio_aggregate_is_idempotent_and_preserves_chunk_results():
    _, _, run, chunks = _media_run()
    first = _remote_result(
        run,
        chunks[0],
        index=0,
        text="hello there",
        data=_transcript_data(
            [
                {"word": "hello", "start": 8.4, "end": 8.7},
                {"word": "there", "start": 9.1, "end": 9.4},
            ]
        ),
    )
    second = _remote_result(
        run,
        chunks[1],
        index=1,
        text="there friend",
        data=_transcript_data(
            [
                {"word": "there", "start": 0.1, "end": 0.4},
                {"word": "friend", "start": 0.7, "end": 1.1},
            ]
        ),
    )

    with patch(
        "pandect.processing.remote.tasks.execute_remote_stage",
        side_effect=AssertionError("aggregate must not invoke a remote adapter"),
    ):
        assert finalize_remote_aggregate(run.pk) is True
    assert finalize_remote_aggregate(run.pk) is False

    aggregate_stage = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE)
    aggregate = aggregate_stage.results.get(
        result_kind=ProcessingResult.Kind.TRANSCRIPT
    )
    assert aggregate.text == "hello there friend"
    assert aggregate.provenance["additional_remote_requests"] == 0
    assert aggregate.provenance["input_result_ids"] == [str(first.pk), str(second.pk)]
    assert (
        run.stages.filter(kind=ProcessingStage.Kind.REMOTE_TRANSCRIPTION).count() == 2
    )
    assert ProcessingResult.objects.filter(pk__in=(first.pk, second.pk)).count() == 2
    run.refresh_from_db()
    assert run.status == ProcessingRun.Status.SUCCEEDED


@pytest.mark.django_db
def test_only_confirmed_local_voice_evidence_reconciles_a_speaker():
    item, local_run, run, chunks = _media_run()
    _remote_result(
        run,
        chunks[0],
        index=0,
        text="identified line",
        data=_transcript_data(
            [],
            utterances=[
                {
                    "start": 5,
                    "end": 6,
                    "transcript": "identified line",
                    "speaker": 7,
                }
            ],
        ),
    )
    subject = Subject.objects.create(label="Local subject")
    observation = IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.VOICE,
        unit_key="stream:0/window:001",
        source_location={"start_ms": 5000, "end_ms": 6000, "stream_index": 0},
        source_sha256=chunks[0].sha256,
        embedding=encode_embedding((1.0, 0.0)),
        embedding_dimension=2,
        feature_space="voice-test-v1",
        processor_name="local-voice-test",
        processor_version="1",
        preprocessing_version="mono16k-v1",
        provenance={
            "processing_run_id": str(local_run.pk),
            "audio_chunk": {"stream_index": 0},
        },
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=0.9,
        quality_details={"status": "usable"},
        sample_duration_ms=1000,
    )
    candidate = IdentityCandidate.objects.create(
        candidate_set_id=run.pk,
        observation=observation,
        subject=subject,
        modality=IdentityObservation.Modality.VOICE,
        feature_space=observation.feature_space,
        gallery_fingerprint="0" * 64,
        rank=1,
        similarity_score=0.8,
        reference_count=3,
        calibration_revision="test-v1",
        decision=IdentityCandidate.Decision.CONFIRMED,
        decided_at=timezone.now(),
    )
    assertion = IdentityAssertion.objects.create(
        candidate=candidate,
        observation=observation,
        subject=subject,
    )

    assert finalize_remote_aggregate(run.pk) is True

    aggregate = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).results.get(
        result_kind=ProcessingResult.Kind.TRANSCRIPT
    )
    utterance = aggregate.data["streams"][0]["utterances"][0]
    assert utterance["chunk_speaker"] == 7
    assert utterance["reconciled_subject_id"] == str(subject.pk)
    assert utterance["voice_assertion_ids"] == [str(assertion.pk)]
    assert utterance["speaker_scope"] == "operator_confirmed_subject"
    reconciliation = aggregate.data["speaker_reconciliation"]
    assert reconciliation["method"] == "campp-timed-complete-link-v1"
    assert reconciliation["operator_confirmation_has_higher_precedence"] is True
    assert reconciliation["operator_confirmed"] == {
        "method": "operator_confirmed_local_voice_intervals",
        "reconciled_record_count": 1,
        "higher_precedence_than_automatic_similarity": True,
    }


@pytest.mark.django_db
def test_confirmed_voice_attribution_is_scoped_to_simultaneous_audio_streams():
    item, local_run, run, chunks = _media_run()
    second_content = b"simultaneous-stream-one"
    second_stream = DerivedArtifact.objects.create(
        stage=chunks[0].stage,
        catalog_item=item,
        file=SimpleUploadedFile("stream-one.flac", second_content),
        role=DerivedArtifact.Role.AUDIO_CHUNK,
        mime_type="audio/flac",
        sha256=sha256_bytes(second_content),
        byte_size=len(second_content),
        stream_index=1,
        source_start_seconds="0.000000",
        source_end_seconds="10.000000",
    )
    ProcessingRun.objects.filter(pk=run.pk).update(
        configuration={
            "artifact_ids": [str(chunks[0].pk), str(second_stream.pk)],
        }
    )
    for index, (artifact, label, speaker) in enumerate(
        ((chunks[0], "stream zero", 4), (second_stream, "stream one", 9))
    ):
        _remote_result(
            run,
            artifact,
            index=index,
            text=label,
            data=_transcript_data(
                [],
                utterances=[
                    {
                        "start": 5,
                        "end": 6,
                        "transcript": label,
                        "speaker": speaker,
                    }
                ],
            ),
        )

    subjects = []
    for stream_index, artifact in enumerate((chunks[0], second_stream)):
        subject = Subject.objects.create(label=f"Subject {stream_index}")
        subjects.append(subject)
        location = {"start_ms": 5000, "end_ms": 6000}
        provenance = {"processing_run_id": str(local_run.pk)}
        if stream_index == 0:
            location["stream_index"] = stream_index
        else:
            provenance["audio_chunk"] = {"stream_index": stream_index}
        observation = IdentityObservation.objects.create(
            catalog_item=item,
            modality=IdentityObservation.Modality.VOICE,
            unit_key=f"stream:{stream_index}/window:001",
            source_location=location,
            source_sha256=artifact.sha256,
            embedding=encode_embedding((1.0, float(stream_index))),
            embedding_dimension=2,
            feature_space="voice-test-v1",
            processor_name="local-voice-test",
            processor_version="1",
            preprocessing_version="mono16k-v1",
            provenance=provenance,
            quality_status=IdentityObservation.QualityStatus.APPROVED,
            quality_score=0.9,
            quality_details={"status": "usable"},
            sample_duration_ms=1000,
        )
        candidate = IdentityCandidate.objects.create(
            candidate_set_id=run.pk,
            observation=observation,
            subject=subject,
            modality=IdentityObservation.Modality.VOICE,
            feature_space=observation.feature_space,
            gallery_fingerprint="0" * 64,
            rank=1,
            similarity_score=0.8,
            reference_count=3,
            calibration_revision="test-v1",
            decision=IdentityCandidate.Decision.CONFIRMED,
            decided_at=timezone.now(),
        )
        IdentityAssertion.objects.create(
            candidate=candidate,
            observation=observation,
            subject=subject,
        )

    assert finalize_remote_aggregate(run.pk) is True

    aggregate = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).results.get(
        result_kind=ProcessingResult.Kind.TRANSCRIPT
    )
    streams = {value["stream_index"]: value for value in aggregate.data["streams"]}
    assert streams[0]["utterances"][0]["reconciled_subject_id"] == str(subjects[0].pk)
    assert streams[1]["utterances"][0]["reconciled_subject_id"] == str(subjects[1].pk)
    assert streams[0]["utterances"][0]["chunk_speaker"] == 4
    assert streams[1]["utterances"][0]["chunk_speaker"] == 9


@pytest.mark.django_db
def test_local_voice_embeddings_reconcile_a_speaker_across_chunks_without_identity():
    item, local_run, run, chunks = _media_run()
    for index, (artifact, speaker, label) in enumerate(
        ((chunks[0], 2, "first turn"), (chunks[1], 8, "second turn"))
    ):
        _remote_result(
            run,
            artifact,
            index=index,
            text=label,
            data=_transcript_data(
                [],
                utterances=[
                    {
                        "start": 2,
                        "end": 4,
                        "transcript": label,
                        "speaker": speaker,
                    }
                ],
            ),
        )
    _voice_observation(
        item,
        local_run,
        chunks[0],
        index=0,
        start_ms=2000,
        end_ms=4000,
        values=(1.0, 0.0),
    )
    _voice_observation(
        item,
        local_run,
        chunks[1],
        index=1,
        start_ms=11000,
        end_ms=13000,
        values=(0.999, 0.01),
    )

    assert finalize_remote_aggregate(run.pk) is True

    aggregate = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).results.get(
        result_kind=ProcessingResult.Kind.TRANSCRIPT
    )
    utterances = aggregate.data["streams"][0]["utterances"]
    assert [value["chunk_speaker"] for value in utterances] == [2, 8]
    assert {value["local_voice_cluster_id"] for value in utterances} == {
        "stream:0/voice-cluster:001"
    }
    assert all(
        value["speaker_scope"] == "stream_local_voice_cluster" for value in utterances
    )
    assert all("reconciled_subject_id" not in value for value in utterances)
    assert Subject.objects.count() == 0
    assert IdentityAssertion.objects.count() == 0

    reconciliation = aggregate.data["speaker_reconciliation"]
    assert reconciliation["reconciled_group_count"] == 2
    assert reconciliation["scores_are_probabilities"] is False
    assert reconciliation["thresholds"] == {
        "required_quality_status": "approved",
        "minimum_quality_score": 0.65,
        "minimum_sample_duration_ms": 1000,
        "minimum_overlap_seconds": 0.25,
        "minimum_observation_overlap_fraction": 0.5,
        "overlap_assignment_margin": 0.2,
        "cosine_similarity_threshold": 0.8,
        "cosine_ambiguity_margin": 0.05,
    }
    cluster = reconciliation["clusters"][0]
    assert cluster["cluster_id"] == "stream:0/voice-cluster:001"
    assert cluster["feature_provenance"]["feature_space"] == "voice-test-v1"
    assert cluster["feature_provenance"]["preprocessing_version"] == "mono16k-v1"
    assert cluster["scores_are_probabilities"] is False
    assert "values" not in cluster["feature_provenance"]
    assert _nested_keys(aggregate.data).isdisjoint({"embedding", "values"})


@pytest.mark.django_db
def test_operator_voice_assertion_overrides_but_preserves_automatic_cluster_evidence():
    item, local_run, run, chunks = _media_run()
    observations = []
    for index, artifact in enumerate(chunks):
        _remote_result(
            run,
            artifact,
            index=index,
            text=f"turn {index}",
            data=_transcript_data(
                [],
                utterances=[
                    {
                        "start": 2,
                        "end": 4,
                        "transcript": f"turn {index}",
                        "speaker": index + 4,
                    }
                ],
            ),
        )
        source_start = round(float(artifact.source_start_seconds) * 1000)
        observations.append(
            _voice_observation(
                item,
                local_run,
                artifact,
                index=index,
                start_ms=source_start + 2000,
                end_ms=source_start + 4000,
                values=(1.0, 0.0),
            )
        )
    subject = Subject.objects.create(label="Confirmed subject")
    candidate = IdentityCandidate.objects.create(
        candidate_set_id=run.pk,
        observation=observations[0],
        subject=subject,
        modality=IdentityObservation.Modality.VOICE,
        feature_space=observations[0].feature_space,
        gallery_fingerprint="0" * 64,
        rank=1,
        similarity_score=0.9,
        reference_count=3,
        calibration_revision="test-v1",
        decision=IdentityCandidate.Decision.CONFIRMED,
        decided_at=timezone.now(),
    )
    assertion = IdentityAssertion.objects.create(
        candidate=candidate,
        observation=observations[0],
        subject=subject,
    )

    assert finalize_remote_aggregate(run.pk) is True

    aggregate = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).results.get(
        result_kind=ProcessingResult.Kind.TRANSCRIPT
    )
    first, second = aggregate.data["streams"][0]["utterances"]
    assert first["local_voice_cluster_id"] == second["local_voice_cluster_id"]
    assert first["reconciled_subject_id"] == str(subject.pk)
    assert first["voice_assertion_ids"] == [str(assertion.pk)]
    assert first["speaker_scope"] == "operator_confirmed_subject"
    assert second["speaker_scope"] == "stream_local_voice_cluster"
    assert aggregate.data["speaker_reconciliation"]["operator_confirmed"] == {
        "method": "operator_confirmed_local_voice_intervals",
        "reconciled_record_count": 1,
        "higher_precedence_than_automatic_similarity": True,
    }


@pytest.mark.django_db
def test_distinct_and_ambiguous_voice_groups_remain_chunk_local():
    item, local_run, run, chunks = _media_run()
    third = _additional_chunk(
        item,
        chunks,
        stream_index=0,
        start=18,
        end=28,
    )
    artifacts = [*chunks, third]
    ProcessingRun.objects.filter(pk=run.pk).update(
        configuration={"artifact_ids": [str(value.pk) for value in artifacts]}
    )
    vectors = (
        (1.0, 0.0),
        (0.3420201433, 0.9396926208),
        (0.8191520443, 0.5735764364),
    )
    starts = (2000, 11000, 20000)
    for index, (artifact, vector, start_ms) in enumerate(
        zip(artifacts, vectors, starts, strict=True)
    ):
        _remote_result(
            run,
            artifact,
            index=index,
            text=f"turn {index}",
            data=_transcript_data(
                [],
                utterances=[
                    {
                        "start": 2,
                        "end": 4,
                        "transcript": f"turn {index}",
                        "speaker": index + 1,
                    }
                ],
            ),
        )
        _voice_observation(
            item,
            local_run,
            artifact,
            index=index,
            start_ms=start_ms,
            end_ms=start_ms + 2000,
            values=vector,
        )

    assert finalize_remote_aggregate(run.pk) is True

    aggregate = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).results.get(
        result_kind=ProcessingResult.Kind.TRANSCRIPT
    )
    utterances = aggregate.data["streams"][0]["utterances"]
    assert all(value["speaker_scope"] == "chunk_local" for value in utterances)
    assert all("local_voice_cluster_id" not in value for value in utterances)
    reconciliation = aggregate.data["speaker_reconciliation"]
    assert reconciliation["clusters"] == []
    ambiguity = next(
        value
        for value in reconciliation["ambiguities"]
        if value["reason"] == "multiple_similarity_clusters"
    )
    assert ambiguity["chunk_speaker"] == 3
    assert len(ambiguity["candidate_minimum_cosines"]) == 2
    assert ambiguity["scores_are_probabilities"] is False


@pytest.mark.django_db
def test_voice_embedding_reconciliation_never_crosses_audio_streams():
    item, local_run, run, chunks = _media_run()
    stream_one = [
        _additional_chunk(
            item,
            chunks,
            stream_index=1,
            start=start,
            end=end,
        )
        for start, end in ((0, 10), (9, 18))
    ]
    artifacts = [*chunks, *stream_one]
    ProcessingRun.objects.filter(pk=run.pk).update(
        configuration={"artifact_ids": [str(value.pk) for value in artifacts]}
    )
    for index, artifact in enumerate(artifacts):
        source_start = round(float(artifact.source_start_seconds) * 1000)
        _remote_result(
            run,
            artifact,
            index=index,
            text=f"stream {artifact.stream_index} chunk {index}",
            data=_transcript_data(
                [],
                utterances=[
                    {
                        "start": 2,
                        "end": 4,
                        "transcript": "same-sounding local voice",
                        "speaker": index + 3,
                    }
                ],
            ),
        )
        _voice_observation(
            item,
            local_run,
            artifact,
            index=index,
            start_ms=source_start + 2000,
            end_ms=source_start + 4000,
            values=(1.0, 0.0),
        )

    assert finalize_remote_aggregate(run.pk) is True

    aggregate = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).results.get(
        result_kind=ProcessingResult.Kind.TRANSCRIPT
    )
    streams = {value["stream_index"]: value for value in aggregate.data["streams"]}
    stream_zero_ids = {
        value["local_voice_cluster_id"] for value in streams[0]["utterances"]
    }
    stream_one_ids = {
        value["local_voice_cluster_id"] for value in streams[1]["utterances"]
    }
    assert stream_zero_ids == {"stream:0/voice-cluster:001"}
    assert stream_one_ids == {"stream:1/voice-cluster:001"}
    assert stream_zero_ids.isdisjoint(stream_one_ids)


@pytest.mark.django_db
def test_voice_feature_version_mismatch_prevents_automatic_reconciliation():
    item, local_run, run, chunks = _media_run()
    for index, artifact in enumerate(chunks):
        _remote_result(
            run,
            artifact,
            index=index,
            text=f"versioned turn {index}",
            data=_transcript_data(
                [],
                utterances=[
                    {
                        "start": 2,
                        "end": 4,
                        "transcript": f"versioned turn {index}",
                        "speaker": index,
                    }
                ],
            ),
        )
        source_start = round(float(artifact.source_start_seconds) * 1000)
        _voice_observation(
            item,
            local_run,
            artifact,
            index=index,
            start_ms=source_start + 2000,
            end_ms=source_start + 4000,
            values=(1.0, 0.0),
            preprocessing_version=f"mono16k-v{index + 1}",
        )

    assert finalize_remote_aggregate(run.pk) is True

    aggregate = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).results.get(
        result_kind=ProcessingResult.Kind.TRANSCRIPT
    )
    utterances = aggregate.data["streams"][0]["utterances"]
    assert all(value["speaker_scope"] == "chunk_local" for value in utterances)
    reconciliation = aggregate.data["speaker_reconciliation"]
    assert reconciliation["clusters"] == []
    assert {value["reason"] for value in reconciliation["unmatched_groups"]} == {
        "incompatible_feature_version"
    }


@pytest.mark.django_db
def test_video_account_combines_descriptions_subtitles_ocr_and_transcript():
    item, local_run, run, chunks = _media_run(video=True)
    subtitle_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.VIDEO_SUBTITLES,
        status=ProcessingStage.Status.SUCCEEDED,
    )
    subtitle = b"1\n00:00:02,000 --> 00:00:03,000\nLocal caption\n"
    DerivedArtifact.objects.create(
        stage=subtitle_stage,
        catalog_item=item,
        file=SimpleUploadedFile("caption.srt", subtitle),
        role=DerivedArtifact.Role.SUBTITLE,
        mime_type="application/x-subrip",
        sha256=sha256_bytes(subtitle),
        byte_size=len(subtitle),
        stream_index=2,
    )
    keyframe_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
        status=ProcessingStage.Status.SUCCEEDED,
    )
    keyframe_content = b"local-keyframe"
    keyframe = DerivedArtifact.objects.create(
        stage=keyframe_stage,
        catalog_item=item,
        file=SimpleUploadedFile("frame-004.jpg", keyframe_content),
        role=DerivedArtifact.Role.KEYFRAME,
        mime_type="image/jpeg",
        sha256=sha256_bytes(keyframe_content),
        byte_size=len(keyframe_content),
        frame_index=4,
        source_start_seconds="4.000000",
        source_end_seconds="4.000000",
        metadata={"metadata_stripped": True},
    )
    frame_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key="frame:004",
        status=ProcessingStage.Status.SUCCEEDED,
    )
    ProcessingResult.objects.create(
        stage=frame_stage,
        result_kind=ProcessingResult.Kind.OCR,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        data={
            "frames": [
                {
                    "timestamp_ms": 4000,
                    "frame_index": 4,
                    "artifact_id": str(keyframe.pk),
                    "ocr": {"text": "LOCAL SIGN"},
                }
            ]
        },
        processor_name="local-ocr",
        processor_version="1",
    )
    description_stage = ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.REMOTE_IMAGE_DESCRIPTION,
        unit_key="artifact:frame",
        status=ProcessingStage.Status.SUCCEEDED,
        attempts=1,
        max_attempts=1,
    )
    description = ProcessingResult.objects.create(
        stage=description_stage,
        result_kind=ProcessingResult.Kind.IMAGE_DESCRIPTION,
        evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
        text="A person enters.",
        data={"description": "A person enters."},
        processor_name="remote-vision",
        processor_version="1",
        provenance={
            "artifact_id": str(keyframe.pk),
            "frame_index": 1,
            "source_start_seconds": "1.000000",
            "source_end_seconds": "1.000000",
        },
    )
    transcript = _remote_result(
        run,
        chunks[0],
        index=1,
        text="spoken line",
        data=_transcript_data(
            [],
            utterances=[
                {"start": 5, "end": 6, "transcript": "spoken line", "speaker": 0}
            ],
        ),
    )

    assert finalize_ready_remote_aggregates() == 1

    aggregate_stage = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE)
    account = aggregate_stage.results.get(
        result_kind=ProcessingResult.Kind.VIDEO_DESCRIPTION
    )
    assert [entry["kind"] for entry in account.data["entries"]] == [
        "keyframe_description",
        "subtitle",
        "visible_text",
        "transcript",
    ]
    assert account.text == (
        "[00:00:01.000] Keyframe description: A person enters.\n"
        "[00:00:02.000] Subtitle: Local caption\n"
        "[00:00:04.000] Visible text: LOCAL SIGN\n"
        "[00:00:05.000] Transcript: spoken line"
    )
    assert account.data["additional_remote_requests"] == 0
    assert set(account.provenance["input_result_ids"]) == {
        str(description.pk),
        str(transcript.pk),
    }


@pytest.mark.django_db
def test_video_account_binds_qr_correlated_face_attributes_and_current_identity():
    item, local_run, run, chunks = _media_run(video=True)
    keyframe_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.VIDEO_KEYFRAMES,
        status=ProcessingStage.Status.SUCCEEDED,
    )
    keyframe_content = b"metadata-free-frame"
    keyframe = DerivedArtifact.objects.create(
        stage=keyframe_stage,
        catalog_item=item,
        file=SimpleUploadedFile("frame-004.jpg", keyframe_content),
        role=DerivedArtifact.Role.KEYFRAME,
        mime_type="image/jpeg",
        sha256=sha256_bytes(keyframe_content),
        byte_size=len(keyframe_content),
        frame_index=4,
        source_start_seconds="4.000000",
        source_end_seconds="4.000000",
        metadata={"metadata_stripped": True},
    )
    child_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key="frame:004",
        status=ProcessingStage.Status.SUCCEEDED,
    )
    qr = ProcessingResult.objects.create(
        stage=child_stage,
        result_kind=ProcessingResult.Kind.QR_CODES,
        evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
        data={
            "frames": [
                {
                    "timestamp_ms": 4000,
                    "frame_index": 4,
                    "artifact_id": str(keyframe.pk),
                    "codes": [
                        {
                            "value": "LOCAL://case/7",
                            "polygon": [[1, 2], [10, 2], [10, 11], [1, 11]],
                        }
                    ],
                }
            ]
        },
        processor_name="opencv-qrcode-video-frames",
        processor_version="1",
        input_hashes={"artifact": keyframe.sha256},
    )
    face = ProcessingResult.objects.create(
        stage=child_stage,
        result_kind=ProcessingResult.Kind.FACE_OBSERVATIONS,
        evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
        data={
            "frames": [
                {
                    "timestamp_ms": 4000,
                    "frame_index": 4,
                    "artifact_id": str(keyframe.pk),
                    "faces": [
                        {
                            "track_id": 1,
                            "bbox": [20, 30, 80, 90],
                            "quality_score": 0.88,
                            "quality_assessment": "usable",
                            "apparent_facial_expression_scores": {
                                "angry": 0.05,
                                "disgust": 0.05,
                                "fearful": 0.05,
                                "happy": 0.5,
                                "neutral": 0.2,
                                "sad": 0.1,
                                "surprised": 0.05,
                            },
                            "apparent_age_band": "30-44",
                            "binary_visual_class_scores": {
                                "class_0": 0.4,
                                "class_1": 0.6,
                            },
                            "head_pose_degrees": {
                                "yaw": 4.0,
                                "pitch": -2.0,
                                "roll": 1.0,
                            },
                            # A malformed producer must never make an opaque
                            # feature public through deterministic aggregation.
                            "embedding": [0.6, 0.8],
                        }
                    ],
                }
            ]
        },
        processor_name="pandect.video-face-track",
        processor_version="1",
        input_hashes={"artifact": keyframe.sha256},
    )
    member = IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.FACE,
        unit_key="track:001",
        source_location={
            "bbox": [20, 30, 80, 90],
            "frame_index": 4,
            "timestamp_ms": 4000,
        },
        source_sha256=keyframe.sha256,
        embedding=encode_embedding((0.6, 0.8)),
        embedding_dimension=2,
        feature_space="opencv-sface:test",
        processor_name="pandect.video-face-track",
        processor_version="1",
        preprocessing_version="tracking-v1",
        provenance={
            "processing_run_id": str(local_run.pk),
            "processing_stage_id": str(child_stage.pk),
            "identity_evidence_role": VIDEO_FACE_FRAME_ROLE,
        },
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=0.88,
        quality_details={"quality_assessment": "usable"},
    )
    aggregate_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
        unit_key="aggregate",
        status=ProcessingStage.Status.SUCCEEDED,
    )
    aggregate_face = ProcessingResult.objects.create(
        stage=aggregate_stage,
        result_kind=ProcessingResult.Kind.FACE_OBSERVATIONS,
        evidence_tier=ProcessingResult.EvidenceTier.MODEL_GENERATED,
        data={
            "tracks": [
                {
                    "track_id": 1,
                    "frame_count": 1,
                    "representative_frame_index": 4,
                    "representative_timestamp_ms": 4000,
                    "quality_score": 0.88,
                    "quality_assessment": "usable",
                    "member_observation_ids": [str(member.pk)],
                }
            ],
            "correlation": {
                "revision": "greedy-sface-cosine-v1",
                "scores_are_probabilities": False,
            },
        },
        processor_name="pandect.video-face-track",
        processor_version="1",
    )
    aggregate_observation = IdentityObservation.objects.create(
        catalog_item=item,
        modality=IdentityObservation.Modality.FACE,
        unit_key="track:001",
        source_location={
            "bbox": [20, 30, 80, 90],
            "frame_index": 4,
            "timestamp_ms": 4000,
        },
        source_sha256=keyframe.sha256,
        embedding=encode_embedding((0.6, 0.8)),
        embedding_dimension=2,
        feature_space="opencv-sface:test",
        processor_name="pandect.video-face-track",
        processor_version="1",
        preprocessing_version="tracking-v1",
        provenance={
            "processing_run_id": str(local_run.pk),
            "processing_stage_id": str(aggregate_stage.pk),
            "identity_evidence_role": VIDEO_FACE_AGGREGATE_ROLE,
            "video_face_tracking": {
                "revision": "greedy-sface-cosine-v1",
                "member_observation_ids": [str(member.pk)],
            },
        },
        quality_status=IdentityObservation.QualityStatus.APPROVED,
        quality_score=0.88,
        quality_details={
            "quality_assessment": "usable",
            "correlated_frame_count": 1,
        },
    )
    identity_stage = ProcessingStage.objects.create(
        run=local_run,
        kind=ProcessingStage.Kind.FACE_IDENTITY,
        status=ProcessingStage.Status.SUCCEEDED,
    )
    subject = Subject.objects.create(label="Operator label")
    decided_at = timezone.now()
    candidate = IdentityCandidate.objects.create(
        candidate_set_id=uuid4(),
        producing_run=local_run,
        producing_stage=identity_stage,
        observation=aggregate_observation,
        subject=subject,
        modality=IdentityObservation.Modality.FACE,
        feature_space=aggregate_observation.feature_space,
        gallery_fingerprint="c" * 64,
        rank=1,
        similarity_score=0.87,
        reference_count=2,
        calibration_revision="uncalibrated-cosine-v1",
        decision=IdentityCandidate.Decision.CONFIRMED,
        decided_at=decided_at,
    )
    assertion = IdentityAssertion.objects.create(
        candidate=candidate,
        observation=aggregate_observation,
        subject=subject,
        asserted_at=decided_at,
    )
    _remote_result(
        run,
        chunks[0],
        index=0,
        text="spoken",
        data=_transcript_data(
            [],
            utterances=[{"start": 6, "end": 7, "transcript": "spoken", "speaker": 0}],
        ),
    )

    with patch(
        "pandect.processing.remote.aggregation.current_identity_candidates",
        return_value=(candidate,),
    ):
        assert finalize_ready_remote_aggregates() == 1

    account = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).results.get(
        result_kind=ProcessingResult.Kind.VIDEO_DESCRIPTION
    )
    qr_entry = next(
        value for value in account.data["entries"] if value["kind"] == "qr_observation"
    )
    face_entry = next(
        value
        for value in account.data["entries"]
        if value["kind"] == "face_track_observation"
    )
    assert qr_entry["start"] == 4.0
    assert qr_entry["artifact_id"] == str(keyframe.pk)
    assert qr_entry["result_id"] == str(qr.pk)
    assert qr_entry["qr_payload"] == "LOCAL://case/7"
    assert face_entry["start"] == face_entry["end"] == 4.0
    assert face_entry["result_ids"] == sorted([str(face.pk), str(aggregate_face.pk)])
    assert face_entry["artifact_ids"] == [str(keyframe.pk)]
    assert face_entry["face_track_observation_id"] == str(aggregate_observation.pk)
    assert face_entry["apparent_facial_expression_score_distribution"]["happy"] == {
        "sample_count": 1,
        "minimum": 0.5,
        "maximum": 0.5,
        "mean": 0.5,
    }
    assert face_entry["apparent_adult_age_band_counts"] == {"30-44": 1}
    assert (
        face_entry["anonymous_binary_visual_class_score_distribution"]["class_1"][
            "mean"
        ]
        == 0.6
    )
    assert face_entry["head_pose_degree_distribution"]["pitch"]["mean"] == -2.0
    assert face_entry["identity_candidates"] == [
        {
            "candidate_id": str(candidate.pk),
            "subject_id": str(subject.pk),
            "subject_label": "Operator label",
            "rank": 1,
            "raw_similarity": 0.87,
            "scores_are_probabilities": False,
            "reference_count": 2,
            "calibration_revision": "uncalibrated-cosine-v1",
            "decision": IdentityCandidate.Decision.CONFIRMED,
            "decided_at": decided_at.isoformat(),
            "assertion_id": str(assertion.pk),
            "evidence_tier": ProcessingResult.EvidenceTier.OPERATOR_CONFIRMED,
            "uncertainty": (
                "Closed-gallery similarity candidate; the raw score is not a "
                "probability or open-world identity conclusion."
            ),
        }
    ]
    assert _nested_keys(account.data).isdisjoint({"embedding", "values"})
    assert set(account.input_hashes["local_results"]) == {
        str(qr.pk),
        str(face.pk),
        str(aggregate_face.pk),
    }
    artifact_binding = account.input_hashes["local_artifacts"][str(keyframe.pk)]
    assert artifact_binding["content_sha256"] == keyframe.sha256
    assert artifact_binding["byte_size"] == keyframe.byte_size
    assert len(artifact_binding["snapshot_sha256"]) == 64
    assert set(account.input_hashes["local_identity_observations"]) == {
        str(aggregate_observation.pk)
    }
    assert set(account.input_hashes["local_identity_candidates"]) == {str(candidate.pk)}
    assert account.provenance["local_input_artifact_ids"] == [str(keyframe.pk)]
    assert account.provenance["local_identity_candidate_ids"] == [str(candidate.pk)]
    assert account.processor_version == "2"
    assert account.data["schema_version"] == "2"

    with patch(
        "pandect.processing.remote.aggregation.current_identity_candidates",
        return_value=(candidate,),
    ):
        assert verify_video_timeline_bindings(account) is True

        later_qr = ProcessingResult.objects.create(
            stage=child_stage,
            result_kind=ProcessingResult.Kind.QR_CODES,
            evidence_tier=ProcessingResult.EvidenceTier.DETERMINISTIC,
            data={"frames": [{"codes": [{"value": "changed"}]}]},
            processor_name="pandect.qr",
            processor_version="2",
        )
        assert str(later_qr.pk) not in account.input_hashes["local_results"]
        assert verify_video_timeline_bindings(account) is True

        with keyframe.file.open("wb") as stream:
            stream.write(b"tampered-frame")
        assert verify_video_timeline_bindings(account) is False
        with keyframe.file.open("wb") as stream:
            stream.write(keyframe_content)
        assert verify_video_timeline_bindings(account) is True

        decide_identity_candidate(
            candidate.pk,
            IdentityCandidate.Decision.REJECTED,
            note="Operator corrected the association.",
        )
        assert verify_video_timeline_bindings(account) is False


@pytest.mark.django_db
def test_aggregate_waits_for_all_remote_units_and_recovery_is_bounded():
    _, _, run, chunks = _media_run()
    _remote_result(
        run,
        chunks[0],
        index=0,
        text="complete",
        data=_transcript_data([]),
    )
    ProcessingStage.objects.create(
        run=run,
        kind=ProcessingStage.Kind.REMOTE_TRANSCRIPTION,
        unit_key="pending",
        status=ProcessingStage.Status.QUEUED,
        max_attempts=1,
    )

    assert finalize_remote_aggregate(run.pk) is False
    assert not run.stages.filter(kind=ProcessingStage.Kind.REMOTE_AGGREGATE).exists()

    ProcessingStage.objects.filter(run=run, unit_key="pending").update(
        status=ProcessingStage.Status.FAILED
    )
    assert finalize_ready_remote_aggregates(limit=1) == 1
    assert finalize_ready_remote_aggregates(limit=1) == 0


@pytest.mark.django_db
def test_pending_aggregate_survives_dispatch_gap_and_can_resume():
    _, _, run, chunks = _media_run()
    _remote_result(
        run,
        chunks[0],
        index=0,
        text="durable",
        data=_transcript_data([{"word": "durable", "start": 0.0, "end": 0.5}]),
    )

    with patch(
        "pandect.processing.remote.aggregation.enqueue_stage",
        return_value=False,
    ):
        assert finalize_remote_aggregate(run.pk) is False

    aggregate_stage = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE)
    assert aggregate_stage.status == ProcessingStage.Status.PENDING

    assert enqueue_stage(aggregate_stage.pk) is True
    aggregate_stage.refresh_from_db()
    assert aggregate_stage.status == ProcessingStage.Status.SUCCEEDED
    assert aggregate_stage.results.get().text == "durable"


@pytest.mark.django_db
def test_queued_aggregate_keeps_run_nonterminal_until_worker_completes():
    _, _, run, chunks = _media_run()
    _remote_result(
        run,
        chunks[0],
        index=0,
        text="queued",
        data=_transcript_data([{"word": "queued", "start": 0, "end": 0.5}]),
    )

    with patch(
        "pandect.processing.services._dispatch_stage_task",
        return_value=object(),
    ):
        assert finalize_remote_aggregate(run.pk) is True

    run.refresh_from_db()
    stage = run.stages.get(kind=ProcessingStage.Kind.REMOTE_AGGREGATE)
    assert run.status == ProcessingRun.Status.RUNNING
    assert stage.status == ProcessingStage.Status.QUEUED
    assert finalize_remote_aggregate(run.pk) is True

    assert enqueue_stage(stage.pk) is False
    stage.refresh_from_db()
    assert stage.status == ProcessingStage.Status.QUEUED

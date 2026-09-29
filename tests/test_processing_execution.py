import hashlib
import math
import uuid
import wave
from array import array
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.test import override_settings

from pandect.processing import execution
from pandect.processing.execution import (
    _audio_metadata,
    _audio_split,
    _binary_version_snapshot,
    _face_analysis,
    _image_metadata,
    _image_ocr,
    _image_qr,
    _image_visual,
    _image_visual_payload,
    _provenance,
    _publish_artifact,
    _representative_frame_limit,
    _storage_paths,
    _video_audio,
    _video_frame_analysis,
    _video_keyframes,
    _video_metadata,
)
from pandect.processing.extractors.derived_media import DerivedMediaFile, SceneChange
from pandect.processing.extractors.errors import (
    ModelIntegrityError,
    ToolFailedError,
    UnsupportedPixelFormatError,
)
from pandect.processing.models import DerivedArtifact, ProcessingResult, ProcessingStage
from pandect.processing.services import LOCAL_STAGE_DEFINITIONS, StageProcessingError


class _Artifacts:
    def __init__(self, values=()):
        self.values = list(values)

    def filter(self, **_filters):
        return self

    def order_by(self, *_fields):
        return list(self.values)


def _stage(tmp_path: Path, *, item_type: str = "video", artifacts=()):
    source = tmp_path / f"source.{item_type}"
    source.write_bytes(b"canonical-source")
    item_id = uuid.uuid4()
    item = SimpleNamespace(
        pk=item_id,
        item_type=item_type,
        derived_artifacts=_Artifacts(artifacts),
    )
    setattr(item, item_type, SimpleNamespace(file=SimpleNamespace(path=str(source))))
    run_id = uuid.uuid4()
    run = SimpleNamespace(
        pk=run_id,
        catalog_item_id=item_id,
        catalog_item=item,
        source_fingerprint="a" * 64,
        source_size=source.stat().st_size,
        configuration={},
    )
    return SimpleNamespace(pk=uuid.uuid4(), run=run, run_id=run_id, attempts=1)


def _derived(
    path: Path,
    *,
    stream_index=None,
    frame_index=None,
    start=0.0,
    end=1.0,
    mime_type="audio/flac",
    metadata=None,
):
    payload = path.read_bytes()
    return DerivedMediaFile(
        path=str(path),
        mime_type=mime_type,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        stream_index=stream_index,
        frame_index=frame_index,
        source_start_seconds=start,
        source_end_seconds=end,
        metadata=metadata or {"metadata_stripped": True},
    )


@pytest.fixture
def permit_synthetic_stage_publication(monkeypatch):
    monkeypatch.setattr(
        "pandect.processing.execution.heartbeat_stage",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "pandect.processing.execution.ProcessingStage.objects.filter",
        lambda **_kwargs: SimpleNamespace(exists=lambda: True),
    )
    monkeypatch.setattr(
        "pandect.processing.execution.ArtifactPublication.objects.create",
        lambda **_kwargs: SimpleNamespace(delete=lambda: None),
    )


def test_storage_work_file_retains_derivative_suffix(tmp_path):
    stage = _stage(tmp_path)
    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        temporary, final, relative = _storage_paths(stage, "stream-2.flac")

    assert temporary.suffix == ".flac"
    assert temporary.name != "stream-2.flac"
    assert temporary.parent.name == "attempt-1"
    assert final.name.endswith("-stream-2.flac")
    assert relative.startswith(f"derived/{stage.run.catalog_item_id}/{stage.run_id}/")


def test_storage_work_path_rejects_attempt_symlink_to_newer_workspace(tmp_path):
    stage = _stage(tmp_path)
    stage_root = tmp_path / "work" / str(stage.run_id) / str(stage.pk)
    newer_workspace = stage_root / "attempt-2"
    newer_workspace.mkdir(parents=True)
    (newer_workspace / "live.wav").write_bytes(b"live")
    (stage_root / "attempt-1").symlink_to(newer_workspace, target_is_directory=True)

    with (
        override_settings(
            MEDIA_ROOT=tmp_path / "media",
            WORK_ROOT=tmp_path / "work",
        ),
        pytest.raises(StageProcessingError, match="work_path_invalid"),
    ):
        _storage_paths(stage, "stream-2.flac")

    assert (newer_workspace / "live.wav").read_bytes() == b"live"


def test_flac_waveform_decode_returns_one_compact_float_buffer(monkeypatch, tmp_path):
    from pandect.media_runner.operations import read_waveform

    normalized = tmp_path / "normalized.flac"
    normalized.write_bytes(b"input")
    observed = {}

    def fake_decode(_argv, **_kwargs):
        output = Path(_argv[-1])
        observed["argv"] = _argv
        observed["output"] = output
        with wave.open(str(output), "wb") as destination:
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.setframerate(16_000)
            destination.writeframes(array("h", (-32768, 0, 16384, 32767)).tobytes())
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    monkeypatch.setattr("pandect.media_runner.operations.run_command", fake_decode)
    values, sample_rate = read_waveform(normalized, tmp_path)

    assert isinstance(values, array)
    assert values.typecode == "f"
    assert sample_rate == 16_000
    assert list(values) == pytest.approx((-1.0, 0.0, 0.5, 32767 / 32768))
    assert observed["argv"].count("-threads") == 2
    assert ("-filter_threads", "1") in tuple(
        zip(observed["argv"], observed["argv"][1:], strict=False)
    )
    assert ("-filter_complex_threads", "1") in tuple(
        zip(observed["argv"], observed["argv"][1:], strict=False)
    )
    assert observed["output"].parent == tmp_path
    assert not list(tmp_path.glob("*.wav"))


@pytest.mark.django_db
def test_artifact_publication_recovers_an_identical_interrupted_output(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    payload = b"reproducible-derived-evidence"
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    with override_settings(
        WORK_ROOT=tmp_path / "work",
        MEDIA_ROOT=tmp_path / "media",
    ):
        temporary, final, _ = _storage_paths(stage, "stable.flac")
        temporary.write_bytes(payload)
        final.write_bytes(payload)
        assert not _publish_artifact(
            temporary,
            final,
            stage=stage,
            expected_sha256=expected_sha256,
            expected_size_bytes=len(payload),
        )

    assert final.read_bytes() == payload
    # The ordinary stage-workspace cleanup removes the redundant attempt after
    # the recovered StageOutcome has been committed.
    assert temporary.read_bytes() == payload


@pytest.mark.django_db
def test_artifact_publication_never_overwrites_a_mismatched_destination(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    expected = b"new-attempt"
    existing = b"preserved-orphan"
    with override_settings(
        WORK_ROOT=tmp_path / "work",
        MEDIA_ROOT=tmp_path / "media",
    ):
        temporary, final, _ = _storage_paths(stage, "stable.flac")
        temporary.write_bytes(expected)
        final.write_bytes(existing)

        with pytest.raises(StageProcessingError) as failure:
            _publish_artifact(
                temporary,
                final,
                stage=stage,
                expected_sha256=hashlib.sha256(expected).hexdigest(),
                expected_size_bytes=len(expected),
            )

    assert failure.value.code == "artifact_destination_mismatch"
    assert final.read_bytes() == existing
    assert temporary.read_bytes() == expected


@pytest.mark.django_db
def test_artifact_publication_rejects_a_preexisting_symlink(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    payload = b"derived"
    target = tmp_path / "outside.bin"
    with override_settings(
        WORK_ROOT=tmp_path / "work",
        MEDIA_ROOT=tmp_path / "media",
    ):
        temporary, final, _ = _storage_paths(stage, "stable.bin")
        temporary.write_bytes(payload)
        target.write_bytes(payload)
        final.symlink_to(target)

        with pytest.raises(StageProcessingError) as failure:
            _publish_artifact(
                temporary,
                final,
                stage=stage,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_size_bytes=len(payload),
            )

    assert failure.value.code == "artifact_destination_unsafe"
    assert final.is_symlink()
    assert target.read_bytes() == payload


def test_image_visual_payload_labels_literal_facts_and_uncertain_heuristics():
    payload = _image_visual_payload(
        {
            "width": 400,
            "height": 200,
            "aspect_ratio": "2:1",
            "mode": "RGB",
            "has_alpha": False,
            "is_animated": False,
            "frame_count": 1,
            "dominant_colors": [{"hex": "#112233", "proportion": 1.0}],
            "quality": {
                "mean": 30,
                "shadow_clip_proportion": 0.2,
                "highlight_clip_proportion": 0.0,
                "edge_standard_deviation": 5,
            },
        }
    )

    assert payload["literal_observations"]["orientation"] == "landscape"
    assert payload["literal_observations"]["dominant_colors"] == [
        {"hex": "#112233", "proportion": 1.0}
    ]
    heuristics = payload["quality_heuristics"]
    assert heuristics["luminance"]["label"] == "predominantly_dark"
    assert heuristics["clipping"]["substantial_shadow_clipping"] is True
    assert heuristics["edge_detail"]["low_edge_detail"] is True
    assert "consistent with blur" in heuristics["edge_detail"]["interpretation"]
    assert heuristics["compression_artifacts"]["status"] == "not_assessed"
    assert "semantic scene description" in payload["limitations"][0]


@pytest.mark.parametrize(
    ("kind", "processor"),
    [
        (ProcessingStage.Kind.IMAGE_OCR, _image_ocr),
        (ProcessingStage.Kind.IMAGE_QR, _image_qr),
        (ProcessingStage.Kind.IMAGE_VISUAL, _image_visual),
        (ProcessingStage.Kind.FACE_ANALYSIS, _face_analysis),
    ],
)
def test_pixel_dependent_image_processors_report_unsupported_decode(
    monkeypatch,
    tmp_path,
    kind,
    processor,
):
    stage = _stage(tmp_path, item_type="image")
    stage.kind = kind
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(UnsupportedPixelFormatError()),
    )

    with (
        override_settings(
            PROCESSING_MAX_IMAGE_PIXELS=200_000_000,
            PROCESSING_MAX_IMAGE_FRAMES=100,
        ),
        pytest.raises(UnsupportedPixelFormatError),
    ):
        processor(stage)


def test_image_metadata_survives_unsupported_pixels(monkeypatch, tmp_path):
    stage = _stage(tmp_path, item_type="image")
    stage.kind = ProcessingStage.Kind.IMAGE_METADATA
    metadata = SimpleNamespace(
        raw_tags={"EXIF:Make": "Fixture camera"},
        as_dict=lambda: {"raw_tags": {"EXIF:Make": "Fixture camera"}},
    )
    monkeypatch.setattr(
        "pandect.processing.execution.extract_metadata", lambda _path: metadata
    )
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(UnsupportedPixelFormatError()),
    )
    monkeypatch.setattr(
        "pandect.processing.execution.timestamp_claims_from_tags",
        lambda _tags: (),
    )

    outcome = _image_metadata(stage)

    assert outcome.results[0].data == {
        "technical": None,
        "metadata": {"raw_tags": {"EXIF:Make": "Fixture camera"}},
        "pixel_analysis_warning": "unsupported_pixel_format",
    }
    assert outcome.timestamp_claims == ()


def test_image_ocr_processes_each_bounded_page_as_a_single_unit(monkeypatch, tmp_path):
    Image = pytest.importorskip("PIL.Image")
    stage = _stage(tmp_path, item_type="image")
    stage.kind = ProcessingStage.Kind.IMAGE_OCR
    source = Path(stage.run.catalog_item.image.file.path)
    pages = [Image.new("RGB", (3, 2), color) for color in ("white", "black", "red")]
    pages[0].save(source, format="TIFF", save_all=True, append_images=pages[1:])
    observed = []

    def fake_extract(path, **options):
        with Image.open(path) as submitted:
            observed.append(
                {
                    "path": Path(path),
                    "format": submitted.format,
                    "frame_count": int(getattr(submitted, "n_frames", 1)),
                    "size": submitted.size,
                    "info": dict(submitted.info),
                    "options": options,
                }
            )
        unit_number = len(observed)
        return SimpleNamespace(
            as_dict=lambda: {
                "text": f"page {unit_number}",
                "words": [],
                "lines": [],
                "blocks": [],
                "page_count": 1,
                "mean_confidence": 90.0,
            },
            text=f"page {unit_number}",
            mean_confidence=90.0,
        )

    monkeypatch.setattr("pandect.processing.execution.extract_ocr", fake_extract)
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    with override_settings(
        WORK_ROOT=tmp_path / "work",
        PROCESSING_MAX_IMAGE_PIXELS=100,
        PROCESSING_MAX_IMAGE_FRAMES=100,
    ):
        outcome = _image_ocr(stage)

    assert len(observed) == 3
    assert all(value["path"] != source for value in observed)
    attempt_workspace = (
        tmp_path / "work" / str(stage.run_id) / str(stage.pk) / "attempt-1"
    )
    assert all(value["path"].parent == attempt_workspace for value in observed)
    assert all(value["format"] == "PNG" for value in observed)
    assert all(value["frame_count"] == 1 for value in observed)
    assert all(value["size"] == (3, 2) for value in observed)
    assert all(value["info"] == {} for value in observed)
    assert all(value["options"] == {"timeout_seconds": 60} for value in observed)
    payload = outcome.results[0].data
    assert payload["source_scope"] == {
        "policy": "bounded_all_frames_or_pages",
        "available_units": 3,
        "processed_units": 3,
        "processed_unit_indexes": [0, 1, 2],
        "analysis_truncated": False,
        "truncation_reason": None,
        "technical_preflight_truncated": False,
        "technical_preflight_truncation_reason": None,
        "timeout_seconds_per_unit": 60,
    }
    assert [value["source_unit_index"] for value in payload["units"]] == [0, 1, 2]
    assert all(
        value["rendered_input"]["metadata_stripped"] for value in payload["units"]
    )
    assert outcome.results[0].text == "page 1\n\npage 2\n\npage 3"


def test_image_qr_and_face_analysis_process_all_metadata_free_source_units(
    monkeypatch,
    tmp_path,
):
    Image = pytest.importorskip("PIL.Image")
    stage = _stage(tmp_path, item_type="image")
    source = Path(stage.run.catalog_item.image.file.path)
    frames = [Image.new("RGB", (4, 3), color) for color in ("red", "green", "blue")]
    frames[0].save(source, format="TIFF", save_all=True, append_images=frames[1:])
    real_analyze_image = execution.analyze_image
    preflight_calls = []
    submitted = {"qr": [], "face": []}

    def observed_preflight(*args, **kwargs):
        preflight_calls.append((args, kwargs))
        return real_analyze_image(*args, **kwargs)

    def inspect_single_unit(path, processor):
        with Image.open(path) as image:
            submitted[processor].append(
                {
                    "path": Path(path),
                    "format": image.format,
                    "frame_count": int(getattr(image, "n_frames", 1)),
                    "size": image.size,
                    "info": dict(image.info),
                }
            )

    def decode_qr(path):
        inspect_single_unit(path, "qr")
        unit_index = len(submitted["qr"]) - 1
        return [
            {
                "value": f"unit-{unit_index}",
                "polygon": [[0.0, 0.0], [3.0, 0.0], [3.0, 2.0], [0.0, 2.0]],
            }
        ]

    class FakeFace:
        unit_key = "face:001"
        bbox = (1.0, 0.5, 2.0, 2.0)
        embedding = (1.0, 0.0)
        feature_space = "opencv-sface:fixture"
        quality_score = 0.9
        quality_assessment = "usable"
        quality_details = {"signal": "clear"}
        provenance = {
            "models": {
                "sface": {
                    "identifier": "opencv-sface-2021dec",
                    "sha256": "b" * 64,
                }
            }
        }

        def as_dict(self):
            return {
                "unit_key": self.unit_key,
                "bbox": list(self.bbox),
                "embedding_dimension": len(self.embedding),
                "feature_space": self.feature_space,
                "provenance": dict(self.provenance),
            }

    def analyze_face_unit(path, **_kwargs):
        inspect_single_unit(path, "face")
        return SimpleNamespace(
            observations=(FakeFace(),),
            warnings=(),
            provenance={"processor": "fixture-face-models"},
        )

    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image", observed_preflight
    )
    monkeypatch.setattr("pandect.processing.execution._decode_qr_codes", decode_qr)
    monkeypatch.setattr("pandect.processing.execution.analyze_faces", analyze_face_unit)
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    with override_settings(
        WORK_ROOT=tmp_path / "work",
        MODELS_ROOT=tmp_path / "models",
        PROCESSING_MAX_IMAGE_PIXELS=100,
        PROCESSING_MAX_IMAGE_FRAMES=100,
    ):
        qr_outcome = _image_qr(stage)
        face_outcome = _face_analysis(stage)

    assert len(preflight_calls) == 2  # Once for each independently durable stage.
    for processor in ("qr", "face"):
        assert len(submitted[processor]) == 3
        assert all(value["path"] != source for value in submitted[processor])
        assert all(value["format"] == "PNG" for value in submitted[processor])
        assert all(value["frame_count"] == 1 for value in submitted[processor])
        assert all(value["size"] == (4, 3) for value in submitted[processor])
        assert all(value["info"] == {} for value in submitted[processor])

    qr_payload = qr_outcome.results[0].data
    assert qr_payload["source_scope"]["processed_unit_indexes"] == [0, 1, 2]
    assert [code["source_unit_index"] for code in qr_payload["codes"]] == [0, 1, 2]
    assert all(
        code["source_unit_geometry"] == {"width": 4, "height": 3, "pixel_count": 12}
        for code in qr_payload["codes"]
    )
    assert all(
        unit["rendered_input"]["metadata_stripped"] for unit in qr_payload["units"]
    )

    face_payload = face_outcome.results[0].data
    assert face_payload["source_scope"]["processed_unit_indexes"] == [0, 1, 2]
    assert [value["source_unit_index"] for value in face_payload["observations"]] == [
        0,
        1,
        2,
    ]
    assert [value["unit_key"] for value in face_payload["observations"]] == [
        "unit:000/face:001",
        "unit:001/face:001",
        "unit:002/face:001",
    ]
    assert all("embedding" not in value for value in face_payload["observations"])
    assert all(
        "embedding" not in value
        for unit in face_payload["units"]
        for value in unit["face_analysis"]["observations"]
    )
    assert [
        value.source_location["source_unit_index"]
        for value in face_outcome.identity_observations
    ] == [0, 1, 2]
    assert all(
        value.source_location["source_unit_geometry"]
        == {"width": 4, "height": 3, "pixel_count": 12}
        for value in face_outcome.identity_observations
    )
    assert all(value.embedding for value in face_outcome.identity_observations)


@pytest.mark.parametrize("processor", (_image_qr, _face_analysis))
@pytest.mark.parametrize(
    ("max_pixels", "max_frames", "reason"),
    ((32, 100, "total_pixel_limit"), (100, 2, "frame_limit")),
)
def test_multiframe_qr_and_face_analysis_record_preflight_truncation(
    monkeypatch,
    tmp_path,
    processor,
    max_pixels,
    max_frames,
    reason,
):
    Image = pytest.importorskip("PIL.Image")
    stage = _stage(tmp_path, item_type="image")
    source = Path(stage.run.catalog_item.image.file.path)
    frames = [Image.new("RGB", (4, 4), color) for color in ("red", "green", "blue")]
    frames[0].save(source, format="TIFF", save_all=True, append_images=frames[1:])
    real_analyze_image = execution.analyze_image
    preflight_calls = []

    def observed_preflight(*args, **kwargs):
        preflight_calls.append((args, kwargs))
        return real_analyze_image(*args, **kwargs)

    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image", observed_preflight
    )
    monkeypatch.setattr(
        "pandect.processing.execution._decode_qr_codes",
        lambda _path: [],
    )
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_faces",
        lambda *_args, **_kwargs: SimpleNamespace(
            observations=(),
            warnings=(),
            provenance={"processor": "fixture-face-models"},
        ),
    )
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    with override_settings(
        WORK_ROOT=tmp_path / "work",
        MODELS_ROOT=tmp_path / "models",
        PROCESSING_MAX_IMAGE_PIXELS=max_pixels,
        PROCESSING_MAX_IMAGE_FRAMES=max_frames,
    ):
        outcome = processor(stage)

    assert len(preflight_calls) == 1
    scope = outcome.results[0].data["source_scope"]
    assert scope == {
        "policy": "bounded_all_frames_or_pages",
        "available_units": 3,
        "processed_units": 2,
        "processed_unit_indexes": [0, 1],
        "analysis_truncated": True,
        "truncation_reason": reason,
        "technical_preflight_truncated": True,
        "technical_preflight_truncation_reason": reason,
    }
    assert [unit["source_unit_index"] for unit in outcome.results[0].data["units"]] == [
        0,
        1,
    ]


def test_binary_revision_probe_is_fixed_bounded_cached_and_failure_tolerant(
    monkeypatch,
):
    calls = []

    def fake_run(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(
            stdout=b"ffmpeg version 8.0-test\nignored build details\n",
            stderr=b"",
        )

    _binary_version_snapshot.cache_clear()
    monkeypatch.setattr(execution, "run_command", fake_run)
    assert _binary_version_snapshot("ffmpeg") == {
        "available": True,
        "version": "ffmpeg version 8.0-test",
    }
    assert _binary_version_snapshot("ffmpeg")["available"] is True
    assert calls == [
        (
            ("ffmpeg", "-version"),
            {"timeout_seconds": 3, "output_limit_bytes": 64 * 1024},
        )
    ]

    _binary_version_snapshot.cache_clear()
    monkeypatch.setattr(
        execution,
        "run_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ToolFailedError()),
    )
    assert _binary_version_snapshot("ffmpeg") == {
        "available": False,
        "version": "unavailable",
    }
    _binary_version_snapshot.cache_clear()


def test_result_provenance_records_safe_effective_runtime_and_parameters(
    monkeypatch, tmp_path
):
    stage = _stage(tmp_path)
    stage.kind = ProcessingStage.Kind.VIDEO_KEYFRAMES
    monkeypatch.setattr(
        execution,
        "_binary_version_snapshot",
        lambda name: {"available": True, "version": f"{name} 1.2.3"},
    )
    monkeypatch.setattr(
        execution,
        "_library_version_snapshot",
        lambda name: {"available": True, "version": f"{name} 4.5.6"},
    )

    with override_settings(PROCESSING_MAX_VIDEO_FRAMES=73):
        value = _provenance(stage, "ffprobe+ffmpeg")

    assert value["runtime_versions"]["binaries"] == {
        "ffmpeg": {"available": True, "version": "ffmpeg 1.2.3"},
        "ffprobe": {"available": True, "version": "ffprobe 1.2.3"},
    }
    assert value["effective_parameters"]["representative_frames"] == {
        "maximum_frames": 73,
        "scene_change_threshold": 0.3,
        "scene_score_field": "lavfi.scene_score",
        "scene_candidate_order": "score_descending_timestamp_ascending",
        "jpeg_quality": 2,
        "metadata_stripped": True,
        "artifact_stream_index": "absolute_ffprobe_stream_index",
        "artifact_frame_index": "representative_selection_ordinal",
        "decoded_source_coordinates": "not_available",
    }
    assert value["effective_parameters"]["subprocess"]["shell"] is False
    assert value["effective_parameters"]["ffmpeg"]["decoder_threads"] == 1
    assert value["effective_parameters"]["ffmpeg"]["encoder_threads"] == 1
    assert value["effective_parameters"]["ffmpeg"]["filter_threads"] == 1
    assert value["effective_parameters"]["ffmpeg"]["filter_complex_threads"] == 1
    assert "source.video" not in repr(value)
    assert "canonical-source" not in repr(value)


def _probe_with_artwork():
    streams = [
        {
            "index": 0,
            "codec_type": "audio",
            "codec_name": "aac",
            "disposition": {},
            "tags": {},
        },
        {
            "index": 2,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1},
            "tags": {"title": "Front cover"},
        },
    ]
    payload = {
        "raw": {"streams": streams, "format": {}},
        "technical": {
            "format": {"duration_seconds": 8.0},
            "streams": streams,
            "chapters": [],
        },
    }
    return SimpleNamespace(
        streams=streams,
        technical=payload["technical"],
        as_dict=lambda: payload,
    )


@pytest.mark.parametrize(
    ("item_type", "processor", "result_kind"),
    [
        ("audio", _audio_metadata, ProcessingResult.Kind.AUDIO_METADATA),
        ("video", _video_metadata, ProcessingResult.Kind.VIDEO_METADATA),
    ],
)
@pytest.mark.django_db
def test_media_metadata_extracts_and_locally_analyzes_attached_artwork(
    monkeypatch,
    tmp_path,
    permit_synthetic_stage_publication,
    item_type,
    processor,
    result_kind,
):
    stage = _stage(tmp_path, item_type=item_type)
    probe = _probe_with_artwork()
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution.timestamp_claims_from_ffprobe", lambda _probe: ()
    )
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)

    def fake_extract(_source, destination, **options):
        destination.write_bytes(b"jpeg-cover")
        return _derived(
            destination,
            stream_index=options["stream_index"],
            start=None,
            end=None,
            mime_type="image/jpeg",
            metadata={
                "attached_picture": True,
                "extraction_mode": "stream_copy",
                "metadata_stripped": False,
            },
        )

    monkeypatch.setattr(
        "pandect.processing.execution.extract_embedded_artwork", fake_extract
    )
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image",
        lambda *_args, **_kwargs: SimpleNamespace(
            as_dict=lambda: {"width": 64, "height": 64}
        ),
    )
    monkeypatch.setattr(
        "pandect.processing.execution.extract_ocr",
        lambda _path, **_kwargs: SimpleNamespace(
            as_dict=lambda: {"text": "Cover words", "words": []}
        ),
    )
    monkeypatch.setattr(
        "pandect.processing.execution._decode_qr_codes",
        lambda _path: [{"value": "local-value", "polygon": []}],
    )

    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
        PROCESSING_MAX_IMAGE_PIXELS=200_000_000,
        PROCESSING_MAX_IMAGE_FRAMES=100,
    ):
        outcome = processor(stage)

    assert [value.result_kind for value in outcome.results] == [
        result_kind,
        ProcessingResult.Kind.IMAGE_METADATA,
        ProcessingResult.Kind.OCR,
        ProcessingResult.Kind.QR_CODES,
    ]
    assert len(outcome.artifacts) == 1
    artifact = outcome.artifacts[0]
    assert artifact.role == DerivedArtifact.Role.EMBEDDED_ARTWORK
    assert artifact.stream_index == 2
    assert artifact.sha256 == hashlib.sha256(b"jpeg-cover").hexdigest()
    assert artifact.metadata["ffprobe_pixel_preflight"]["status"] == "not_available"
    assert (
        "post_extraction_pixel_preflight_required"
        in artifact.metadata["ffprobe_pixel_preflight"]["limitation"]
    )
    assert artifact.metadata["remote_transfer_planned"] is False
    artwork = outcome.results[0].data["embedded_artwork"]
    assert artwork["artifacts"][0]["artifact_sha256"] == artifact.sha256
    assert artwork["warnings"] == []
    assert outcome.results[1].data["images"][0]["technical"]["width"] == 64
    assert outcome.results[2].text == "Cover words"
    assert outcome.results[3].data["images"][0]["codes"][0]["value"] == "local-value"
    assert all(
        result.input_hashes["embedded_artwork"] == [artifact.sha256]
        for result in outcome.results[1:]
    )


@pytest.mark.django_db
def test_attached_artwork_declared_over_pixel_limit_is_not_extracted(
    monkeypatch, tmp_path
):
    stage = _stage(tmp_path, item_type="audio")
    probe = _probe_with_artwork()
    probe.streams[1]["width"] = 20
    probe.streams[1]["height"] = 30
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution.timestamp_claims_from_ffprobe", lambda _probe: ()
    )
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    monkeypatch.setattr(
        "pandect.processing.execution.extract_embedded_artwork",
        lambda *_args, **_kwargs: pytest.fail(
            "FFmpeg must not run for declared oversized attached artwork"
        ),
    )

    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
        PROCESSING_MAX_IMAGE_PIXELS=599,
    ):
        outcome = _audio_metadata(stage)

    assert outcome.artifacts == ()
    artwork = outcome.results[0].data["embedded_artwork"]
    assert artwork["artifacts"] == []
    assert artwork["declared_streams"][0]["pixel_preflight"] == {
        "status": "exceeds_limit",
        "declared_width": 20,
        "declared_height": 30,
        "declared_pixel_count": 600,
        "maximum_pixels": 599,
        "limitation": None,
    }
    assert artwork["warnings"] == [
        {
            "stream_index": 2,
            "analyzer": "ffprobe_pixel_preflight",
            "code": "image_pixel_limit",
            "declared_width": 20,
            "declared_height": 30,
            "declared_pixel_count": 600,
            "maximum_pixels": 599,
        }
    ]


@pytest.mark.django_db
def test_unsupported_artwork_pixels_do_not_fail_audio_metadata(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path, item_type="audio")
    probe = _probe_with_artwork()
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution.timestamp_claims_from_ffprobe", lambda _probe: ()
    )
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)

    def fake_extract(_source, destination, **options):
        destination.write_bytes(b"unsupported-pixels")
        return _derived(
            destination,
            stream_index=options["stream_index"],
            start=None,
            end=None,
            mime_type="image/jpeg",
        )

    monkeypatch.setattr(
        "pandect.processing.execution.extract_embedded_artwork", fake_extract
    )
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ToolFailedError()),
    )
    monkeypatch.setattr(
        "pandect.processing.execution.extract_ocr",
        lambda _path: pytest.fail("OCR must not bypass the bounded image preflight"),
    )
    monkeypatch.setattr(
        "pandect.processing.execution._decode_qr_codes",
        lambda _path: pytest.fail("QR must not bypass the bounded image preflight"),
    )

    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
        PROCESSING_MAX_IMAGE_PIXELS=200_000_000,
        PROCESSING_MAX_IMAGE_FRAMES=100,
    ):
        outcome = _audio_metadata(stage)

    assert outcome.results[0].result_kind == ProcessingResult.Kind.AUDIO_METADATA
    assert len(outcome.artifacts) == 1
    assert outcome.results[0].data["embedded_artwork"]["warnings"] == [
        {
            "stream_index": 2,
            "artifact_sha256": outcome.artifacts[0].sha256,
            "mime_type": "image/jpeg",
            "ffprobe_pixel_preflight": {
                "status": "not_available",
                "declared_width": None,
                "declared_height": None,
                "declared_pixel_count": None,
                "maximum_pixels": 200_000_000,
                "limitation": (
                    "ffprobe_did_not_report_complete_positive_attached_picture_"
                    "dimensions; bounded_post_extraction_pixel_preflight_required"
                ),
            },
            "analyzer": "image",
            "code": "tool_failed",
        }
    ]
    assert outcome.results[1].data["images"] == []
    assert outcome.results[2].data["images"] == []
    assert outcome.results[3].data["images"] == []


def test_video_stage_map_fans_audio_and_faces_into_identity_work():
    definitions = {
        definition.kind: definition.dependencies
        for definition in LOCAL_STAGE_DEFINITIONS["video"]
    }

    assert definitions[ProcessingStage.Kind.AUDIO_SPLIT] == (
        ProcessingStage.Kind.VIDEO_AUDIO,
    )
    assert definitions[ProcessingStage.Kind.AUDIO_ACOUSTICS] == (
        ProcessingStage.Kind.VIDEO_AUDIO,
    )
    assert definitions[ProcessingStage.Kind.VOICE_ANALYSIS] == (
        ProcessingStage.Kind.AUDIO_SPLIT,
    )
    assert definitions[ProcessingStage.Kind.VOICE_IDENTITY] == (
        ProcessingStage.Kind.VOICE_ANALYSIS,
    )
    assert definitions[ProcessingStage.Kind.FACE_IDENTITY] == (
        ProcessingStage.Kind.VIDEO_FRAME_ANALYSIS,
    )


@pytest.mark.django_db
def test_audio_split_materializes_every_audio_stream(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    probe = SimpleNamespace(
        technical={"format": {"duration_seconds": 20.0}},
        streams=[
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "duration_seconds": 12.0,
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "opus",
                "duration_seconds": 20.0,
            },
        ],
        chapters=[],
    )
    heartbeat_calls = []
    extracted_streams = []
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution.detect_silence", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        "pandect.processing.execution._heartbeat",
        lambda value: heartbeat_calls.append(value.pk),
    )

    def fake_extract(_source, destination, **options):
        destination.write_bytes(f"stream-{options['stream_index']}".encode())
        extracted_streams.append(options["stream_index"])
        return _derived(
            destination,
            stream_index=options["stream_index"],
            start=options["start_seconds"],
            end=options["end_seconds"],
        )

    monkeypatch.setattr(
        "pandect.processing.execution.extract_normalized_audio_chunk", fake_extract
    )
    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        outcome = _audio_split(stage)

    assert extracted_streams == [1, 2]
    assert [artifact.stream_index for artifact in outcome.artifacts] == [1, 2]
    assert all(
        artifact.role == DerivedArtifact.Role.AUDIO_CHUNK
        for artifact in outcome.artifacts
    )
    assert [value["stream_index"] for value in outcome.results[0].data["streams"]] == [
        1,
        2,
    ]
    assert len(heartbeat_calls) == 4


@pytest.mark.django_db
def test_audio_split_reencodes_oversized_chunks_until_each_payload_fits(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    probe = SimpleNamespace(
        technical={"format": {"duration_seconds": 8.0}},
        streams=[
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "flac",
                "duration_seconds": 8.0,
            }
        ],
        chapters=[],
    )
    encoded_ranges = []
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution.detect_silence", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    monkeypatch.setattr("pandect.processing.execution.MAX_TRANSCRIPTION_AUDIO_BYTES", 4)

    def fake_extract(_source, destination, **options):
        start = options["start_seconds"]
        end = options["end_seconds"]
        duration = end - start
        encoded_ranges.append((start, end, options["overlap_before_seconds"]))
        size = 8 if duration > 4 else max(1, math.ceil(duration))
        destination.write_bytes(b"x" * size)
        return _derived(
            destination,
            stream_index=options["stream_index"],
            start=start,
            end=end,
            metadata={
                "metadata_stripped": True,
                "sample_rate": 16000,
                "channels": 1,
                "codec": "flac",
                "resampling": "16000_hz",
                "downmix": "mono",
                "overlap_before_seconds": options["overlap_before_seconds"],
            },
        )

    monkeypatch.setattr(
        "pandect.processing.execution.extract_normalized_audio_chunk", fake_extract
    )
    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        outcome = _audio_split(stage)

    assert encoded_ranges == [
        (0.0, 8.0, 0.0),
        (0.0, 4.0, 0.0),
        (3.0, 8.0, 1.0),
        (3.0, 5.5, 1.0),
        (4.5, 8.0, 1.0),
    ]
    assert [
        (float(value.source_start_seconds), float(value.source_end_seconds))
        for value in outcome.artifacts
    ] == [(0.0, 4.0), (3.0, 5.5), (4.5, 8.0)]
    assert all(value.byte_size <= 4 for value in outcome.artifacts)
    assert [value.metadata["chunk_index"] for value in outcome.artifacts] == [0, 1, 2]
    assert [value.metadata["provider_split_path"] for value in outcome.artifacts] == [
        "L",
        "RL",
        "RR",
    ]
    assert all(
        value.metadata["provider_payload_limit_bytes"] == 4
        for value in outcome.artifacts
    )
    assert all(
        value.metadata["resampling"] == "16000_hz" for value in outcome.artifacts
    )
    stream_result = outcome.results[0].data["streams"][0]
    assert stream_result["provider_split_encodings"] == 5
    assert stream_result["provider_split_encodings_by_initial_chunk"] == {"0": 5}
    assert stream_result["provider_split_applied"] is True


@pytest.mark.django_db
def test_audio_split_encoding_budget_is_scoped_per_initial_chunk(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    probe = SimpleNamespace(
        technical={"format": {"duration_seconds": 1199.0}},
        streams=[
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "flac",
                "duration_seconds": 1199.0,
            }
        ],
        chapters=[],
    )
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution.detect_silence", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    monkeypatch.setattr("pandect.processing.execution.MAX_PROVIDER_SPLIT_ENCODINGS", 1)

    def fake_extract(_source, destination, **options):
        destination.write_bytes(b"fits")
        return _derived(
            destination,
            stream_index=options["stream_index"],
            start=options["start_seconds"],
            end=options["end_seconds"],
        )

    monkeypatch.setattr(
        "pandect.processing.execution.extract_normalized_audio_chunk", fake_extract
    )
    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        outcome = _audio_split(stage)

    stream_result = outcome.results[0].data["streams"][0]
    assert len(outcome.artifacts) == 2
    assert stream_result["provider_split_encodings"] == 2
    assert stream_result["provider_split_encodings_by_initial_chunk"] == {
        "0": 1,
        "1": 1,
    }


@pytest.mark.django_db
def test_audio_split_fails_safely_when_oversized_segment_cannot_be_split(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    probe = SimpleNamespace(
        technical={"format": {"duration_seconds": 2.0}},
        streams=[
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "flac",
                "duration_seconds": 2.0,
            }
        ],
        chapters=[],
    )
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution.detect_silence", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    monkeypatch.setattr("pandect.processing.execution.MAX_TRANSCRIPTION_AUDIO_BYTES", 1)

    def fake_extract(_source, destination, **options):
        destination.write_bytes(b"xx")
        return _derived(
            destination,
            stream_index=options["stream_index"],
            start=options["start_seconds"],
            end=options["end_seconds"],
        )

    monkeypatch.setattr(
        "pandect.processing.execution.extract_normalized_audio_chunk", fake_extract
    )
    with (
        override_settings(
            MEDIA_ROOT=tmp_path / "media",
            WORK_ROOT=tmp_path / "work",
        ),
        pytest.raises(StageProcessingError) as failure,
    ):
        _audio_split(stage)

    assert failure.value.code == "audio_chunk_payload_limit"
    assert not list((tmp_path / "media").rglob("*.flac"))


def test_keyframe_budget_is_intelligent_and_never_exceeds_ceiling(settings):
    settings.PROCESSING_MAX_VIDEO_FRAMES = 120

    assert _representative_frame_limit(10, ()) == 2
    assert _representative_frame_limit(60 * 10, ()) == 11
    assert _representative_frame_limit(60 * 60 * 24, tuple(range(500))) == 120


def test_video_keyframe_plan_persists_scored_selection_before_extraction(
    monkeypatch, tmp_path
):
    stage = _stage(tmp_path)
    probe = SimpleNamespace(
        technical={
            "format": {"duration_seconds": 10.0},
            "streams": [
                {
                    "index": 3,
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "disposition": {"attached_pic": 1},
                },
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "disposition": {"attached_pic": 0},
                },
            ],
        }
    )
    heartbeat_calls = []
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution.detect_scene_changes",
        lambda *_args, **_kwargs: (SceneChange(4.0, 0.875),),
    )
    monkeypatch.setattr(
        "pandect.processing.execution._heartbeat",
        lambda value: heartbeat_calls.append(value.pk),
    )

    monkeypatch.setattr(
        "pandect.processing.execution.extract_video_frame",
        lambda *_args, **_kwargs: pytest.fail("planning must not extract frames"),
    )
    with override_settings(PROCESSING_MAX_VIDEO_FRAMES=120):
        outcome = _video_keyframes(stage)

    assert not outcome.artifacts
    assert heartbeat_calls == [stage.pk]
    result = outcome.results[0]
    assert result.data["phase"] == "plan"
    assert result.data["selected_frame_count"] == 3
    assert [value["selection_reason"] for value in result.data["frames"]] == [
        "first_frame",
        "scene_change",
        "last_frame",
    ]
    assert result.data["frames"][1] == {
        "unit_key": "frame:001",
        "selection_index": 1,
        "timestamp_seconds": 4.0,
        "source_stream_index": 0,
        "selection_reason": "scene_change",
        "scene_change_score": 0.875,
    }
    assert result.data["scene_candidates"] == [
        {"timestamp_seconds": 4.0, "score": 0.875}
    ]
    assert result.data["selection_policy"]["scene_score_field"] == ("lavfi.scene_score")
    assert [value["index"] for value in result.data["video_streams"]] == [0]


@pytest.mark.django_db
def test_video_keyframe_unit_is_metadata_stripped_traceable_and_heartbeated(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    stage.unit_key = "frame:001"
    heartbeat_calls = []
    plan_result = SimpleNamespace(
        pk=uuid.uuid4(),
        stage_id=uuid.uuid4(),
        stage=SimpleNamespace(unit_key="plan"),
    )
    coordinate = {
        "unit_key": "frame:001",
        "selection_index": 1,
        "timestamp_seconds": 4.0,
        "source_stream_index": 0,
        "selection_reason": "scene_change",
        "scene_change_score": 0.875,
    }
    monkeypatch.setattr(
        "pandect.processing.execution._keyframe_plan_coordinate",
        lambda _stage: (
            plan_result,
            {"duration_seconds": 10.0},
            coordinate,
        ),
    )
    monkeypatch.setattr(
        "pandect.processing.execution._heartbeat",
        lambda value: heartbeat_calls.append(value.pk),
    )

    def fake_frame(_source, destination, **options):
        destination.write_bytes(f"frame-{options['frame_index']}".encode())
        return _derived(
            destination,
            stream_index=options["video_stream_index"],
            frame_index=options["frame_index"],
            start=options["timestamp_seconds"],
            end=options["timestamp_seconds"],
            mime_type="image/jpeg",
            metadata={
                "metadata_stripped": True,
                "jpeg_quality": 2,
                "source_stream_index": options["video_stream_index"],
                "selection_index": options["frame_index"],
                "selection_requested_timestamp_seconds": options["timestamp_seconds"],
                "frame_index_semantics": (
                    "representative_selection_ordinal_not_decoded_source_frame_index"
                ),
                "source_offset_semantics": (
                    "requested_seek_time_not_decoded_source_pts"
                ),
                "decoded_source_pts": "not_available",
                "decoded_source_timestamp_seconds": "not_available",
                "decoded_source_frame_index": "not_available",
                "decoded_coordinate_limitation": (
                    "bounded_single_frame_extraction_does_not_expose_absolute_"
                    "decoded_source_coordinates"
                ),
            },
        )

    monkeypatch.setattr("pandect.processing.execution.extract_video_frame", fake_frame)
    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
        PROCESSING_MAX_VIDEO_FRAMES=120,
    ):
        outcome = _video_keyframes(stage)

    assert len(outcome.artifacts) == 1
    assert heartbeat_calls == [stage.pk]
    artifact = outcome.artifacts[0]
    assert artifact.role == DerivedArtifact.Role.KEYFRAME
    assert artifact.metadata["metadata_stripped"] is True
    assert artifact.stream_index == 0
    assert artifact.frame_index == 1
    assert artifact.metadata["selection_index"] == 1
    assert artifact.metadata["selection_reason"] == "scene_change"
    assert artifact.metadata["scene_change_score"] == 0.875
    assert artifact.metadata["scene_change_threshold"] == 0.3
    assert artifact.metadata["keyframe_plan_result_id"] == str(plan_result.pk)
    assert artifact.metadata["decoded_source_pts"] == "not_available"
    assert (
        artifact.metadata["frame_index_semantics"]
        == "representative_selection_ordinal_not_decoded_source_frame_index"
    )
    assert artifact.source_start_seconds == Decimal("4.0")
    assert outcome.results[0].data["phase"] == "extraction"
    assert outcome.results[0].provenance["keyframe_plan"]["coordinate"] == coordinate


@pytest.mark.django_db
def test_video_audio_keeps_faithful_and_normalized_stream_artifacts_separate(
    monkeypatch, tmp_path, permit_synthetic_stage_publication
):
    stage = _stage(tmp_path)
    stage.kind = ProcessingStage.Kind.VIDEO_AUDIO
    probe = SimpleNamespace(
        technical={
            "format": {"duration_seconds": 20.0},
            "streams": [
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "codec_tag_string": "mp4a",
                    "profile": "LC",
                    "sample_rate": 48_000,
                    "channels": 2,
                    "channel_layout": "stereo",
                    "sample_fmt": "fltp",
                    "bits_per_raw_sample": 24,
                    "duration_seconds": 12.0,
                    "tags": {"language": "eng", "title": "Main"},
                }
            ],
        }
    )
    heartbeat_calls = []
    faithful_calls = []
    normalized_calls = []
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    monkeypatch.setattr(
        "pandect.processing.execution._heartbeat",
        lambda value: heartbeat_calls.append(value.pk),
    )

    def fake_faithful(_source, destination, **options):
        faithful_calls.append((destination, options))
        destination.write_bytes(b"source-aac-packets")
        return _derived(
            destination,
            stream_index=options["stream_index"],
            start=0,
            end=options["duration_seconds"],
            mime_type="audio/aac",
            metadata={
                "metadata_stripped": True,
                "derivative_purpose": "source_faithful_audio_stream",
                "source_stream_index": options["stream_index"],
                "source_codec": options["codec_name"],
                "source_sample_rate": options["sample_rate"],
                "source_channels": options["channels"],
                "extraction_mode": "stream_copy",
                "packet_copy": True,
                "lossless_transcode": False,
                "output_codec": "aac",
                "output_container": "adts",
                "reencoding_loss": "none",
            },
        )

    def fake_normalized(_source, destination, **options):
        normalized_calls.append((destination, options))
        destination.write_bytes(b"mono-16khz-flac")
        return _derived(
            destination,
            stream_index=options["stream_index"],
            start=0,
            end=options["duration_seconds"],
            metadata={
                "metadata_stripped": True,
                "sample_rate": 16_000,
                "channels": 1,
                "codec": "flac",
            },
        )

    monkeypatch.setattr(
        "pandect.processing.execution.extract_faithful_audio_stream", fake_faithful
    )
    monkeypatch.setattr(
        "pandect.processing.execution.extract_normalized_audio_stream",
        fake_normalized,
    )
    with override_settings(
        MEDIA_ROOT=tmp_path / "media",
        WORK_ROOT=tmp_path / "work",
    ):
        outcome = _video_audio(stage)

    assert len(heartbeat_calls) == 2
    assert len(faithful_calls) == len(normalized_calls) == 1
    assert faithful_calls[0][0].suffix == ".aac"
    assert normalized_calls[0][0].suffix == ".flac"
    assert [artifact.role for artifact in outcome.artifacts] == [
        DerivedArtifact.Role.AUDIO_STREAM,
        DerivedArtifact.Role.NORMALIZED_MEDIA,
    ]
    faithful, normalized = outcome.artifacts
    assert faithful.stream_index == normalized.stream_index == 2
    assert faithful.sha256 != normalized.sha256
    assert faithful.metadata["source_codec"] == "aac"
    assert faithful.metadata["source_sample_rate"] == 48_000
    assert faithful.metadata["source_channels"] == 2
    assert faithful.metadata["remote_transfer_planned"] is False
    assert normalized.metadata["derivative_purpose"] == (
        "mono_16khz_analysis_audio_stream"
    )
    assert normalized.metadata["source_sample_rate"] == 48_000
    assert normalized.metadata["source_channels"] == 2
    assert normalized.metadata["remote_transfer_planned"] is False
    assert [value["role"] for value in outcome.results[0].data["artifacts"]] == [
        DerivedArtifact.Role.AUDIO_STREAM,
        DerivedArtifact.Role.NORMALIZED_MEDIA,
    ]
    faithful_policy = outcome.results[0].provenance["effective_parameters"][
        "source_faithful_audio"
    ]
    assert faithful_policy["fallback_codec"] == "wavpack"
    assert faithful_policy["preserve_source_sample_rate"] is True
    assert faithful_policy["preserve_source_channel_count"] is True
    assert faithful_policy["remote_transfer_eligible"] is False


@pytest.mark.django_db
def test_video_keyframes_reject_oversized_stream_before_decoding(monkeypatch, tmp_path):
    stage = _stage(tmp_path)
    probe = SimpleNamespace(
        technical={
            "format": {"duration_seconds": 10.0},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "width": 20,
                    "height": 20,
                    "disposition": {"attached_pic": 0},
                }
            ],
        }
    )
    monkeypatch.setattr("pandect.processing.execution.probe_media", lambda _path: probe)
    scene_detection_called = False

    def fail_scene_detection(*_args, **_kwargs):
        nonlocal scene_detection_called
        scene_detection_called = True

    monkeypatch.setattr(
        "pandect.processing.execution.detect_scene_changes",
        fail_scene_detection,
    )

    with (
        override_settings(PROCESSING_MAX_IMAGE_PIXELS=399),
        pytest.raises(StageProcessingError) as failure,
    ):
        _video_keyframes(stage)

    assert failure.value.code == "image_pixel_limit"
    assert scene_detection_called is False


class _FaceObservation:
    def __init__(self, *, quality_score: float):
        self.embedding = (1.0, 0.0)
        self.feature_space = "opencv-sface:unit-test"
        self.quality_score = quality_score
        self.quality_assessment = "usable"
        self.quality_details = {"signal": "clear"}
        self.bbox = (1.0, 2.0, 40.0, 50.0)
        self.provenance = {
            "models": {
                "sface": {
                    "identifier": "opencv-sface-2021dec",
                    "sha256": "b" * 64,
                }
            }
        }

    def as_dict(self):
        return {
            "bbox": list(self.bbox),
            "quality_score": self.quality_score,
            "feature_space": self.feature_space,
        }


def test_video_frame_analysis_correlates_faces_and_keeps_embeddings_opaque(
    monkeypatch, tmp_path
):
    frame_artifacts = []
    for index in range(2):
        path = tmp_path / f"frame-{index}.jpg"
        path.write_bytes(f"jpeg-{index}".encode())
        frame_artifacts.append(
            SimpleNamespace(
                pk=uuid.uuid4(),
                frame_index=index,
                source_start_seconds=Decimal(str(index * 2)),
                metadata={"metadata_stripped": True},
                file=SimpleNamespace(path=str(path)),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                byte_size=path.stat().st_size,
            )
        )
    stage = _stage(tmp_path, artifacts=frame_artifacts)
    heartbeat_calls = []
    monkeypatch.setattr(
        "pandect.processing.execution._heartbeat",
        lambda value: heartbeat_calls.append(value.pk),
    )
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image",
        lambda *_args, **_kwargs: SimpleNamespace(
            as_dict=lambda: {"width": 64, "height": 64}
        ),
    )
    monkeypatch.setattr(
        "pandect.processing.execution.extract_ocr",
        lambda path, **_kwargs: SimpleNamespace(
            as_dict=lambda: {"text": Path(path).stem},
        ),
    )
    monkeypatch.setattr(
        "pandect.processing.execution._decode_qr_codes", lambda _path: []
    )
    observations = iter(
        (
            _FaceObservation(quality_score=0.8),
            _FaceObservation(quality_score=0.9),
        )
    )
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_faces",
        lambda *_args, **_kwargs: SimpleNamespace(observations=(next(observations),)),
    )

    outcome = _video_frame_analysis(stage)

    assert len(heartbeat_calls) == 2
    assert [result.result_kind for result in outcome.results] == [
        ProcessingResult.Kind.IMAGE_VISUAL,
        ProcessingResult.Kind.OCR,
        ProcessingResult.Kind.QR_CODES,
        ProcessingResult.Kind.FACE_OBSERVATIONS,
    ]
    assert len(outcome.identity_observations) == 1
    identity = outcome.identity_observations[0]
    assert identity.unit_key == "track:001"
    assert identity.source_location == {
        "bbox": [1.0, 2.0, 40.0, 50.0],
        "frame_index": 1,
        "timestamp_ms": 2000,
    }
    assert identity.quality_details["correlated_frame_count"] == 2
    assert identity.quality_details["quality_assessment"] == "usable"
    assert identity.provenance["identity_evidence_role"] == "aggregate_face_track"
    face_result = outcome.results[-1]
    assert face_result.data["tracks"] == [
        {
            "track_id": 1,
            "frame_count": 2,
            "representative_frame_index": 1,
            "representative_timestamp_ms": 2000,
            "quality_score": 0.9,
            "quality_assessment": "usable",
        }
    ]
    assert "embedding" not in repr(face_result.data).casefold()


def test_video_frame_analysis_exposes_required_face_model_integrity_failure(
    monkeypatch, tmp_path
):
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"jpeg")
    artifact = SimpleNamespace(
        pk=uuid.uuid4(),
        frame_index=0,
        source_start_seconds=Decimal("0"),
        metadata={"metadata_stripped": True},
        file=SimpleNamespace(path=str(path)),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        byte_size=path.stat().st_size,
    )
    stage = _stage(tmp_path, artifacts=[artifact])
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image",
        lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "pandect.processing.execution.extract_ocr",
        lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "pandect.processing.execution._decode_qr_codes", lambda _path: []
    )
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_faces",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ModelIntegrityError()),
    )

    with pytest.raises(ModelIntegrityError):
        _video_frame_analysis(stage)


def test_video_frame_analysis_rejects_changed_keyframe_before_analysis(
    monkeypatch, tmp_path
):
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"recorded-frame")
    artifact = SimpleNamespace(
        pk=uuid.uuid4(),
        frame_index=0,
        source_start_seconds=Decimal("0"),
        metadata={"metadata_stripped": True},
        file=SimpleNamespace(path=str(path)),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        byte_size=path.stat().st_size,
    )
    stage = _stage(tmp_path, artifacts=[artifact])
    path.write_bytes(b"changed-frame")
    analyzed = False

    def fail_if_analyzed(*_args, **_kwargs):
        nonlocal analyzed
        analyzed = True

    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    monkeypatch.setattr("pandect.processing.execution.analyze_image", fail_if_analyzed)

    with pytest.raises(StageProcessingError) as failure:
        _video_frame_analysis(stage)

    assert failure.value.code == "derived_artifact_integrity"
    assert analyzed is False


def test_video_frame_analysis_rechecks_keyframe_after_analysis(monkeypatch, tmp_path):
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"recorded-frame")
    artifact = SimpleNamespace(
        pk=uuid.uuid4(),
        frame_index=0,
        source_start_seconds=Decimal("0"),
        metadata={"metadata_stripped": True},
        file=SimpleNamespace(path=str(path)),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        byte_size=path.stat().st_size,
    )
    stage = _stage(tmp_path, artifacts=[artifact])
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_image",
        lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "pandect.processing.execution.extract_ocr",
        lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "pandect.processing.execution._decode_qr_codes", lambda _path: []
    )

    def mutate_during_analysis(*_args, **_kwargs):
        path.write_bytes(b"changed-during-analysis")
        return SimpleNamespace(observations=())

    monkeypatch.setattr(
        "pandect.processing.execution.analyze_faces", mutate_during_analysis
    )

    with pytest.raises(StageProcessingError) as failure:
        _video_frame_analysis(stage)

    assert failure.value.code == "derived_artifact_integrity"


def _voice_chunk_stage(tmp_path, *, content=b"recorded-chunk"):
    path = tmp_path / "chunk.flac"
    path.write_bytes(content)
    artifact = SimpleNamespace(
        pk=uuid.uuid4(),
        stream_index=1,
        source_start_seconds=Decimal("0"),
        source_end_seconds=Decimal("1"),
        metadata={"chunk_index": 0},
        file=SimpleNamespace(path=str(path)),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        byte_size=path.stat().st_size,
    )
    stage = _stage(tmp_path, artifacts=[artifact])
    stage.unit_key = "stream:1/chunk:000"
    return stage, artifact, path


def test_voice_analysis_rejects_changed_audio_chunk_before_decode(
    monkeypatch, tmp_path
):
    stage, _artifact, path = _voice_chunk_stage(tmp_path)
    path.write_bytes(b"changed-chunk")
    decoded = False

    def fail_if_decoded(*_args, **_kwargs):
        nonlocal decoded
        decoded = True

    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)
    monkeypatch.setattr(
        "pandect.processing.execution.analyze_voice_file", fail_if_decoded
    )

    with pytest.raises(StageProcessingError) as failure:
        execution._voice_analysis(stage)

    assert failure.value.code == "derived_artifact_integrity"
    assert decoded is False


def test_voice_analysis_rechecks_audio_chunk_after_inference(monkeypatch, tmp_path):
    stage, _artifact, path = _voice_chunk_stage(tmp_path)
    monkeypatch.setattr("pandect.processing.execution._heartbeat", lambda _stage: None)

    def mutate_during_inference(*_args, **_kwargs):
        path.write_bytes(b"changed-during-inference")
        return SimpleNamespace(
            speech_regions=(),
            observations=(),
            provenance={
                "models": {"campplus": {"identifier": "test", "sha256": "b" * 64}}
            },
        )

    monkeypatch.setattr(
        "pandect.processing.execution.analyze_voice_file", mutate_during_inference
    )

    with pytest.raises(StageProcessingError) as failure:
        execution._voice_analysis(stage)

    assert failure.value.code == "derived_artifact_integrity"

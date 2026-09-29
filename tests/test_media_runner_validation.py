"""A compromised decoder cannot amplify tiny metadata into unbounded work."""

import copy
import json
from dataclasses import asdict

import pytest

from pandect.media_runner.validation import (
    MAX_BOUNDARIES,
    MAX_IMAGE_FRAMES,
    MAX_MEDIA_SECONDS,
    MAX_OBSERVATIONS,
    MAX_STREAMS,
    bind_model_provenance,
    validate_operation_result,
)
from pandect.processing.extractors.errors import InvalidExtractorOutputError
from pandect.processing.extractors.exiftool import parse_exiftool_json
from pandect.processing.extractors.ffprobe import parse_ffprobe_json
from pandect.processing.extractors.tesseract import parse_tesseract_tsv


def probe_result(duration=600):
    return asdict(
        parse_ffprobe_json(
            json.dumps(
                {
                    "format": {"duration": str(duration), "nb_streams": 1},
                    "streams": [
                        {"index": 0, "codec_type": "audio", "codec_name": "flac"}
                    ],
                }
            )
        )
    )


def image_result():
    return {
        "technical": {
            "width": 10,
            "height": 20,
            "pixel_count": 200,
            "frame_count": 1,
            "frames_analyzed": 1,
            "decoded_pixel_count": 200,
            "frames": [{"index": 0, "width": 10, "height": 20}],
        }
    }


@pytest.mark.parametrize("version", ["", "tool\nforged log", "\x1b[2J", "tool\x00"])
def test_tool_version_rejects_terminal_controls_and_empty_values(version):
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("tool_version", {"version": version})


@pytest.mark.parametrize("duration", [0, 600, 24 * 3600, MAX_MEDIA_SECONDS])
def test_probe_accepts_bounded_long_recordings(duration):
    validate_operation_result("probe_media", probe_result(duration))


@pytest.mark.parametrize(
    "duration",
    [
        True,
        -1,
        float("nan"),
        float("inf"),
        MAX_MEDIA_SECONDS + 1,
        1e300,
        10**400,
        "600",
    ],
)
def test_probe_rejects_amplifying_or_mistyped_duration(duration):
    result = probe_result()
    result["technical"]["format"]["duration_seconds"] = duration
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("probe_media", result)


@pytest.mark.parametrize("duration", [MAX_MEDIA_SECONDS + 1, 1e300])
def test_stream_duration_cannot_bypass_container_duration_limit(duration):
    result = probe_result()
    result["technical"]["streams"][0]["duration_seconds"] = duration
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("probe_media", result)


@pytest.mark.parametrize("area", ["raw", "technical"])
@pytest.mark.parametrize(
    ("field", "maximum"),
    [
        ("streams", MAX_STREAMS),
        ("chapters", MAX_BOUNDARIES),
    ],
)
def test_probe_rejects_excessive_stream_or_chapter_fanout(area, field, maximum):
    result = probe_result()
    result[area][field] = [{}] * (maximum + 1)
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("probe_media", result)


@pytest.mark.parametrize("replacement", [None, True, "0", -1, 65536])
def test_probe_requires_valid_stream_indexes(replacement):
    result = probe_result()
    result["technical"]["streams"][0]["index"] = replacement
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("probe_media", result)


def test_probe_rejects_duplicate_stream_indexes():
    result = probe_result()
    result["technical"]["streams"] *= 2
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("probe_media", result)


def test_probe_rejects_bad_nested_shapes_before_orchestration():
    result = probe_result()
    result["technical"]["streams"][0]["tags"] = ["not", "a", "mapping"]
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("probe_media", result)


def test_image_accepts_bounded_consistent_frames():
    validate_operation_result("analyze_image", image_result())


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("width", 0),
        ("height", True),
        ("pixel_count", 201),
        ("decoded_pixel_count", 201),
        ("frames_analyzed", 2),
        ("frame_count", 0),
        ("frames", [{}] * (MAX_IMAGE_FRAMES + 1)),
    ],
)
def test_image_rejects_invalid_or_amplifying_frame_metadata(key, replacement):
    result = image_result()
    result["technical"][key] = replacement
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("analyze_image", result)


def test_image_rejects_repeated_or_nonsequential_indexes():
    result = image_result()
    result["technical"]["frames"][0]["index"] = 99
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("analyze_image", result)


def test_image_rejects_forged_aggregate_decoded_pixel_count():
    result = image_result()
    result["technical"].update(
        {
            "frame_count": 2,
            "frames_analyzed": 2,
            "decoded_pixel_count": 200_000_000,
            "frames": [
                {"index": 0, "width": 10_000, "height": 20_000},
                {"index": 1, "width": 10_000, "height": 20_000},
            ],
        }
    )
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("analyze_image", result)


@pytest.mark.parametrize(
    ("operation", "value"),
    [
        ("detect_silence", [{"start_seconds": 2, "end_seconds": 1, "label": ""}]),
        ("detect_scene_changes", [{"timestamp_seconds": 1, "score": 2}]),
        (
            "detect_scene_changes",
            [{"timestamp_seconds": MAX_MEDIA_SECONDS + 1, "score": 0.9}],
        ),
        ("detect_silence", [{}] * (MAX_BOUNDARIES + 1)),
    ],
)
def test_time_boundaries_are_bounded_and_ordered(operation, value):
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result(operation, value)


def face_result():
    return {
        "observations": [
            {
                "unit_key": "face:0",
                "feature_space": "synthetic",
                "quality_score": 0.9,
                "provenance": {},
                "quality_details": {},
                "embedding": [1.0, 0.0],
                "detector_confidence": 0.9,
                "bbox": [0.0, 0.0, 10.0, 10.0],
            }
        ],
        "warnings": [],
        "provenance": {},
    }


def test_face_accepts_bounded_normalized_embedding():
    validate_operation_result("analyze_faces", face_result())


@pytest.mark.parametrize(
    "embedding", [[], [0, 0], [True], [float("inf")], [2.0], [0.0] * 4097]
)
def test_face_rejects_unsafe_embedding_dimensions_and_values(embedding):
    result = face_result()
    result["observations"][0]["embedding"] = embedding
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("analyze_faces", result)


def test_face_rejects_unbounded_gallery_fanout():
    result = face_result()
    result["observations"] *= MAX_OBSERVATIONS + 1
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("analyze_faces", result)


def test_voice_rejects_unbounded_source_interval():
    result = {
        "observations": [],
        "warnings": [],
        "provenance": {},
        "speech_regions": [{"start_ms": 0, "end_ms": 601_001}],
        "non_speech_regions": [],
    }
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("analyze_voice_file", result)


def test_real_parser_result_shapes_remain_supported():
    metadata = asdict(
        parse_exiftool_json(
            json.dumps(
                [
                    {
                        "EXIF:GPSLatitude": 43.0,
                        "EXIF:GPSLongitude": -79.0,
                        "EXIF:GPSAltitude": 10,
                    }
                ]
            )
        )
    )
    validate_operation_result("extract_metadata", metadata)
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t90\tSynthetic\n"
    )
    validate_operation_result("extract_ocr", asdict(parse_tesseract_tsv(tsv)))


def test_metadata_gps_rejects_impossible_coordinates():
    result = {"raw_tags": {}, "gps": {"latitude": 91, "longitude": 0}}
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("extract_metadata", result)


def test_derived_files_validate_claims_without_trusting_hash_or_path():
    result = {
        "sha256": "a" * 64,
        "size_bytes": 10,
        "source_start_seconds": 0,
        "source_end_seconds": 1,
    }
    validate_operation_result("extract_audio_chunk", result)
    validate_operation_result("extract_normalized_audio_chunk", result)
    invalid = copy.deepcopy(result)
    invalid["source_end_seconds"] = MAX_MEDIA_SECONDS + 1
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("extract_normalized_audio_chunk", invalid)


def test_optional_no_subtitle_is_explicit_not_a_general_null_result_bypass():
    validate_operation_result("extract_subtitle_stream", None)
    with pytest.raises(InvalidExtractorOutputError):
        validate_operation_result("extract_audio_chunk", None)


@pytest.mark.parametrize("operation", ["analyze_faces", "analyze_voice_file"])
def test_model_attribution_and_feature_space_are_selected_by_trusted_code(
    monkeypatch, tmp_path, operation
):
    from pandect.processing.extractors.model_files import LocalModelProvenance
    from pandect.processing.model_manifest import MODEL_ARTIFACTS

    manifest = {artifact.filename: artifact for artifact in MODEL_ARTIFACTS}
    checked_files = []

    def verify(path, *, identifier, expected_sha256):
        assert path.parent == tmp_path
        artifact = manifest[path.name]
        assert expected_sha256 == artifact.sha256
        checked_files.append(path.name)
        return LocalModelProvenance(
            identifier, artifact.sha256, artifact.byte_size, True
        )

    monkeypatch.setattr(
        "pandect.processing.extractors.model_files.validate_local_model", verify
    )
    forged = {
        "models": {"sface": {"sha256": "f" * 64, "identifier": "forged"}},
        "processor": "forged",
        "runtime": {"version": "forged"},
    }
    value = {
        "provenance": forged,
        "observations": [
            {
                "provenance": forged,
                "feature_space": "attacker-selected-space",
            }
        ],
    }
    bind_model_provenance(operation, value, tmp_path)
    face = operation == "analyze_faces"
    assert len(checked_files) == (7 if face else 2)
    provenance = value["provenance"]
    assert provenance["processor"] != "forged"
    assert provenance["runtime"]["version"] != "forged"
    for descriptor in provenance["models"].values():
        artifact = manifest[descriptor["filename"]]
        assert descriptor["sha256"] == artifact.sha256
        assert descriptor["revision"] == artifact.revision
        assert descriptor["identifier"] != "forged"
    assert value["observations"][0]["provenance"] == provenance
    assert value["observations"][0]["feature_space"].startswith(
        "opencv-sface-2021dec:" if face else "campp-zh-en-16k:"
    )
    assert provenance["result_trust"] == "untrusted-media-child-output"


def test_model_attribution_does_not_claim_verification_when_models_are_absent(tmp_path):
    from pandect.processing.extractors.errors import ModelUnavailableError

    with pytest.raises(ModelUnavailableError):
        bind_model_provenance("analyze_faces", face_result(), tmp_path)

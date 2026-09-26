"""Production adapters fail closed; unit algorithms use explicit test contexts."""

import json
import wave
from dataclasses import asdict
from pathlib import Path

import pytest

from pandect.media_runner.client import RunnerBusy, RunnerJobError, RunnerUnavailable
from pandect.media_runner.operations import dispatch
from pandect.media_runner.routing import (
    child_execution,
    decode_result,
    operation_timeout,
)
from pandect.processing.extractors import (
    audio,
    derived_media,
    exiftool,
    faces,
    ffprobe,
    images,
    tesseract,
    voice,
)
from pandect.processing.extractors.errors import (
    InvalidExtractorOutputError,
    ModelIntegrityError,
)

pytestmark = pytest.mark.media_runner


class ReachedTransport(Exception):
    pass


@pytest.mark.parametrize(
    ("function", "operation", "parameters", "destination"),
    [
        (ffprobe.probe_media, "probe_media", {}, False),
        (exiftool.extract_metadata, "extract_metadata", {}, False),
        (tesseract.extract_ocr, "extract_ocr", {}, False),
        (images.analyze_image, "analyze_image", {}, False),
        (images.render_image_frame, "render_image_frame", {}, True),
        (audio.detect_silence, "detect_silence", {"duration_seconds": 1}, False),
        (audio.analyze_acoustics, "analyze_acoustics", {"duration_seconds": 1}, False),
        (
            audio.extract_audio_chunk,
            "extract_audio_chunk",
            {"start_seconds": 0, "end_seconds": 1},
            True,
        ),
        (
            derived_media.detect_scene_changes,
            "detect_scene_changes",
            {"duration_seconds": 1},
            False,
        ),
        (
            derived_media.extract_video_frame,
            "extract_video_frame",
            {"timestamp_seconds": 0, "frame_index": 0, "duration_seconds": 1},
            True,
        ),
        (
            derived_media.extract_normalized_audio_stream,
            "extract_normalized_audio_stream",
            {"stream_index": 0, "duration_seconds": 1},
            True,
        ),
        (
            derived_media.extract_faithful_audio_stream,
            "extract_faithful_audio_stream",
            {"stream_index": 0, "duration_seconds": 1, "codec_name": "aac"},
            True,
        ),
        (
            derived_media.extract_normalized_audio_chunk,
            "extract_normalized_audio_chunk",
            {"stream_index": 0, "start_seconds": 0, "end_seconds": 1},
            True,
        ),
        (
            derived_media.extract_embedded_artwork,
            "extract_embedded_artwork",
            {"stream_index": 0, "duration_seconds": 1, "codec_name": "png"},
            True,
        ),
        (
            derived_media.extract_subtitle_stream,
            "extract_subtitle_stream",
            {"stream_index": 0, "duration_seconds": 1, "codec_name": "subrip"},
            True,
        ),
        (
            faces.analyze_faces,
            "analyze_faces",
            {"model_paths": faces.FaceModelPaths.from_root(Path("/private/models"))},
            False,
        ),
        (voice.analyze_voice_file, "analyze_voice_file", {}, False),
    ],
)
def test_native_extractors_route_fixed_operations(
    monkeypatch, tmp_path, function, operation, parameters, destination
):
    source = tmp_path / "source.media"
    source.write_bytes(b"must not be decoded in the caller")
    output = tmp_path / "output.media"

    def transport(name, **options):
        assert name == operation
        assert options["inputs"] == {"source": source}
        assert options["expected_outputs"] == (
            {"artifact": output.name} if destination else {}
        )
        assert not set(options["parameters"]) & {
            "model_paths",
            "expected_hashes",
            "runtime",
            "runner",
            "ffmpeg_binary",
        }
        raise ReachedTransport

    monkeypatch.setattr("pandect.media_runner.client.run_operation", transport)
    with pytest.raises(ReachedTransport):
        function(source, *((output,) if destination else ()), **parameters)


@pytest.mark.parametrize("failure", [RunnerBusy("busy"), RunnerUnavailable("offline")])
def test_no_local_fallback_when_runner_cannot_accept_work(
    monkeypatch, tmp_path, failure
):
    def unavailable(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("pandect.media_runner.client.run_operation", unavailable)
    monkeypatch.setattr(images, "_pillow", lambda: pytest.fail("local decoder called"))
    with pytest.raises(type(failure)):
        images.analyze_image(tmp_path / "evidence.png")


def test_job_failure_preserves_only_known_sanitized_error_code(monkeypatch, tmp_path):
    def invalid_model(*_args, **_kwargs):
        raise RunnerJobError("generic failure", code="local_model_integrity")

    monkeypatch.setattr("pandect.media_runner.client.run_operation", invalid_model)
    with pytest.raises(ModelIntegrityError):
        faces.analyze_faces(
            tmp_path / "image", model_paths=faces.FaceModelPaths.from_root(tmp_path)
        )


def test_media_runtime_constructors_require_child_context(tmp_path):
    with pytest.raises(RuntimeError, match="confined"):
        images._pillow()
    with pytest.raises(RuntimeError, match="confined"):
        faces.OpenCVFaceRuntime(faces.FaceModelPaths.from_root(tmp_path))
    with pytest.raises(RuntimeError, match="confined"):
        voice.SherpaOnnxVoiceRuntime(voice.VoiceModelPaths.from_root(tmp_path))


@pytest.mark.parametrize(
    "parameter",
    ["runner", "ffmpeg_binary", "model_paths", "expected_hashes", "runtime", "path"],
)
def test_child_rejects_execution_and_path_parameters(tmp_path, parameter):
    with child_execution(), pytest.raises(InvalidExtractorOutputError):
        dispatch(
            "probe_media",
            {"source": tmp_path / "input"},
            {parameter: "/etc/passwd"},
            tmp_path,
        )


def test_typed_results_reject_shape_nonfinite_and_boolean_numbers():
    for value in (float("inf"), float("nan"), True, "42"):
        with pytest.raises(InvalidExtractorOutputError):
            decode_result(float, value)
    with pytest.raises(InvalidExtractorOutputError):
        decode_result(ffprobe.FFProbeMetadata, {"raw": {}})
    with pytest.raises(InvalidExtractorOutputError):
        decode_result(tuple[float, float], [1.0])


def test_bitmap_subtitles_do_not_request_or_expect_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "pandect.media_runner.client.run_operation",
        lambda *_a, **_k: pytest.fail("bitmap extraction requested"),
    )
    result = derived_media.extract_subtitle_stream(
        tmp_path / "source",
        tmp_path / "unused",
        stream_index=0,
        codec_name="hdmv_pgs_subtitle",
        duration_seconds=1,
    )
    assert result is None


def test_derived_hash_and_path_are_recomputed_by_trusted_caller(monkeypatch, tmp_path):
    output = tmp_path / "frame.jpg"
    source = tmp_path / "source"
    source.write_bytes(b"source")

    def response(*_args, **_kwargs):
        output.write_bytes(b"new bytes")
        return {
            "value": asdict(
                derived_media.DerivedMediaFile(
                    path="/attacker/path",
                    mime_type="image/jpeg",
                    sha256="0" * 64,
                    size_bytes=999,
                )
            ),
            "outputs": {"artifact": output.name},
        }

    monkeypatch.setattr("pandect.media_runner.client.run_operation", response)
    result = derived_media.extract_video_frame(
        source,
        output,
        timestamp_seconds=0,
        frame_index=0,
        duration_seconds=1,
    )
    assert result.path == str(output)
    assert result.size_bytes == len(b"new bytes")
    assert result.sha256 != "0" * 64


def test_operation_deadlines_are_not_all_two_hours():
    assert operation_timeout("probe_media", {"timeout_seconds": 15}) == 15
    assert operation_timeout("analyze_image", {}) == 120
    assert (
        operation_timeout(
            "extract_ocr", {"timeout_seconds": 60, "include_orientation": True}
        )
        == 120
    )
    assert operation_timeout("extract_video_frame", {"duration_seconds": 60}) == 420


def test_command_runner_cannot_accidentally_parse_media_in_trusted_process():
    from pandect.processing.extractors.subprocesses import run_command

    with pytest.raises(RuntimeError, match="confined"):
        run_command(("ffprobe", "private-evidence"), timeout_seconds=1)


def test_real_results_round_trip_through_typed_operation_contract(
    monkeypatch, tmp_path
):
    """Wire-level unit harness, intentionally not a claim of OS confinement."""
    from PIL import Image, ImageDraw

    image = tmp_path / "synthetic.png"
    pixels = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(pixels).text((20, 30), "SYNTHETIC TEST", fill="black")
    pixels.save(image)
    audio_path = tmp_path / "synthetic.wav"
    with wave.open(str(audio_path), "wb") as audio_output:
        audio_output.setnchannels(1)
        audio_output.setsampwidth(2)
        audio_output.setframerate(16_000)
        audio_output.writeframes(b"\x00\x00" * 1600)

    def simulated_transport(operation, *, inputs, parameters, output_dir, **_options):
        with child_execution():
            result, outputs = dispatch(
                operation, inputs, parameters, output_dir or tmp_path
            )
        result["outputs"] = {key: path.name for key, path in outputs.items()}
        return json.loads(json.dumps(result, allow_nan=False))

    monkeypatch.setattr(
        "pandect.media_runner.client.run_operation", simulated_transport
    )
    metadata = images.analyze_image(image)
    assert metadata.technical["width"] == 320
    assert (
        ffprobe.probe_media(audio_path).technical["streams"][0]["sample_rate"] == 16_000
    )
    assert isinstance(exiftool.extract_metadata(image).raw_tags, dict)
    assert isinstance(tesseract.extract_ocr(image, include_orientation=False).text, str)
    rendered = tmp_path / "rendered.png"
    assert images.render_image_frame(image, rendered)["width"] == 320
    chunk = tmp_path / "chunk.flac"
    result = audio.extract_audio_chunk(
        audio_path, chunk, start_seconds=0, end_seconds=0.1
    )
    assert result.size_bytes == chunk.stat().st_size


def test_compose_smoke_synthetic_fixtures_and_operation_assertions(
    monkeypatch, tmp_path
):
    """Validate fixtures locally; Compose separately runs the same code over IPC."""
    from pandect.media_runner.smoke import check_media

    def simulated_transport(operation, *, inputs, parameters, output_dir, **_options):
        with child_execution():
            result, outputs = dispatch(
                operation, inputs, parameters, output_dir or tmp_path
            )
        result["outputs"] = {key: path.name for key, path in outputs.items()}
        return json.loads(json.dumps(result, allow_nan=False))

    monkeypatch.setattr(
        "pandect.media_runner.client.run_operation", simulated_transport
    )
    check_media(tmp_path)

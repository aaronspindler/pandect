"""Closed list of child-side media operations; no Django or database imports."""

import inspect
import re
import struct
import wave
from array import array
from dataclasses import asdict, is_dataclass
from pathlib import Path

from pandect.media_runner.routing import child_execution, require_child
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
    OptionalDependencyUnavailableError,
    ResourceLimitError,
    ToolFailedError,
    UnsupportedPixelFormatError,
)
from pandect.processing.extractors.subprocesses import run_command

# Every callable and parameter set is selected by application code, not a
# module/function name supplied over the socket.
_EXTRACTORS = {
    "probe_media": (ffprobe.probe_media, "path", None),
    "extract_metadata": (exiftool.extract_metadata, "path", None),
    "extract_ocr": (tesseract.extract_ocr, "path", None),
    "analyze_image": (images.analyze_image, "path", None),
    "render_image_frame": (images.render_image_frame, "path", "destination"),
    "detect_silence": (audio.detect_silence, "path", None),
    "analyze_acoustics": (audio.analyze_acoustics, "path", None),
    "extract_audio_chunk": (
        audio.extract_audio_chunk,
        "source_path",
        "destination_path",
    ),
    "detect_scene_changes": (derived_media.detect_scene_changes, "source_path", None),
    "extract_video_frame": (
        derived_media.extract_video_frame,
        "source_path",
        "destination_path",
    ),
    "extract_normalized_audio_stream": (
        derived_media.extract_normalized_audio_stream,
        "source_path",
        "destination_path",
    ),
    "extract_faithful_audio_stream": (
        derived_media.extract_faithful_audio_stream,
        "source_path",
        "destination_path",
    ),
    "extract_normalized_audio_chunk": (
        derived_media.extract_normalized_audio_chunk,
        "source_path",
        "destination_path",
    ),
    "extract_embedded_artwork": (
        derived_media.extract_embedded_artwork,
        "source_path",
        "destination_path",
    ),
    "extract_subtitle_stream": (
        derived_media.extract_subtitle_stream,
        "source_path",
        "destination_path",
    ),
    "analyze_faces": (faces.analyze_faces, "image_path", None),
}
_VERSION_ARGUMENTS = {
    "ffmpeg": ("ffmpeg", "-version"),
    "ffprobe": ("ffprobe", "-version"),
    "exiftool": ("exiftool", "-ver"),
    "tesseract": ("tesseract", "--version"),
}
_FORBIDDEN_PARAMETERS = {
    "runner",
    "runtime",
    "model_paths",
    "expected_hashes",
    "progress_callback",
    "ffmpeg_binary",
    "ffprobe_binary",
    "exiftool_binary",
    "tesseract_binary",
}


def _json_value(value):
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def read_waveform(path: Path, output_dir: Path) -> tuple[array, int]:
    """Decode and bound PCM entirely inside the child, including wave parsing."""
    require_child()
    pcm_path = output_dir / "voice-input.wav"
    try:
        run_command(
            (
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
                "-i",
                path,
                "-map_metadata",
                "-1",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-threads",
                "1",
                "-n",
                pcm_path,
            ),
            timeout_seconds=7200,
        )
        with wave.open(str(pcm_path), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getframerate() != 16_000
                or source.getsampwidth() != 2
            ):
                raise InvalidExtractorOutputError
            if source.getnframes() > 601 * 16_000:
                raise ResourceLimitError
            raw = source.readframes(source.getnframes())
        integers = array("h")
        integers.frombytes(raw)
        if struct.pack("=h", 1) != struct.pack("<h", 1):
            integers.byteswap()
        samples = array("f", (value / 32768.0 for value in integers))
        return samples, 16_000
    except (wave.Error, EOFError) as error:
        raise InvalidExtractorOutputError from error
    finally:
        pcm_path.unlink(missing_ok=True)


def decode_qr(path: Path) -> list[dict]:
    require_child()
    try:
        import cv2
    except ImportError as error:
        raise OptionalDependencyUnavailableError from error
    cv2.setNumThreads(1)
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise UnsupportedPixelFormatError
    try:
        detected, values, points, _ = cv2.QRCodeDetector().detectAndDecodeMulti(image)
    except cv2.error as error:
        raise ToolFailedError from error
    return (
        [
            {
                "value": value,
                "polygon": points[index].tolist() if index < len(points) else [],
            }
            for index, value in enumerate(values)
        ]
        if detected and points is not None
        else []
    )


def dispatch(
    operation: str, inputs: dict[str, Path], parameters: dict, output_dir: Path
):
    """Run only the fixed operation after the caller has applied confinement."""
    require_child()
    parameters = dict(parameters)
    if set(parameters) & _FORBIDDEN_PARAMETERS:
        raise InvalidExtractorOutputError
    if operation in _EXTRACTORS:
        if set(inputs) != {"source"}:
            raise InvalidExtractorOutputError
        function, source, destination = _EXTRACTORS[operation]
        allowed = set(inspect.signature(function).parameters) - _FORBIDDEN_PARAMETERS
        allowed -= {source, destination}
        allowed.add("output_name")
        if set(parameters) - allowed:
            raise InvalidExtractorOutputError
        arguments = parameters
        output_name = arguments.pop("output_name", None)
        outputs = {}
        if destination is not None:
            if (
                not isinstance(output_name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", output_name)
                or output_name == "result.json"
            ):
                raise InvalidExtractorOutputError
            arguments[destination] = output_dir / output_name
        elif output_name is not None:
            raise InvalidExtractorOutputError
        arguments[source] = inputs["source"]
        if operation == "analyze_faces":
            arguments["model_paths"] = faces.FaceModelPaths.from_root(Path("/models"))
        value = function(**arguments)
        if destination is not None and value is not None:
            outputs["artifact"] = output_dir / output_name
        value = _json_value(value)
        if isinstance(value, dict) and "path" in value:
            value["path"] = output_name
        return {"value": value}, outputs
    if operation == "analyze_voice_file":
        if set(inputs) != {"source"} or set(parameters) - {"source_start_ms"}:
            raise InvalidExtractorOutputError
        samples, sample_rate = read_waveform(inputs["source"], output_dir)
        value = voice.analyze_voice(
            samples,
            model_paths=voice.VoiceModelPaths.from_root(Path("/models")),
            sample_rate=sample_rate,
            **parameters,
        )
        return {"value": _json_value(value)}, {}
    if operation == "decode_qr":
        if set(inputs) != {"source"} or parameters:
            raise InvalidExtractorOutputError
        return {"codes": decode_qr(inputs["source"])}, {}
    if operation == "tool_version":
        if (
            inputs
            or set(parameters) != {"tool"}
            or parameters["tool"] not in _VERSION_ARGUMENTS
        ):
            raise InvalidExtractorOutputError
        result = run_command(
            _VERSION_ARGUMENTS[parameters["tool"]],
            timeout_seconds=3,
            output_limit_bytes=64 * 1024,
        )
        text = (result.stdout or result.stderr).decode("utf-8", errors="replace")
        line = next(
            (line.strip() for line in text.splitlines() if line.strip()), "unknown"
        )
        return {
            "version": "".join(char for char in line if char.isprintable())[:160]
        }, {}
    if operation == "model_smoke":
        if inputs or parameters:
            raise InvalidExtractorOutputError
        from pandect.processing.provisioning import smoke_infer_models

        checks = smoke_infer_models(Path("/models"))
        return {
            "schema_version": "1",
            "checks": [check.as_dict() for check in checks],
        }, {}
    raise InvalidExtractorOutputError


def run_child_request(request: dict) -> dict:
    with child_execution():
        result, outputs = dispatch(
            request["operation"],
            {name: Path(path) for name, path in request["inputs"].items()},
            request["parameters"],
            Path(request["output_dir"]),
        )
    return {
        "result": result,
        "outputs": {name: path.name for name, path in outputs.items()},
    }

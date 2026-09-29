"""Resource and semantic bounds at the untrusted media-result boundary.

Wire size and Python annotations alone do not constrain orchestration: a tiny
JSON duration could otherwise request effectively unlimited audio split work.
These fixed limits are checked before a result enters the trusted application.
Original evidence is retained when analysis exceeds them.
"""

import math
import re
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path

from pandect.processing.extractors.errors import InvalidExtractorOutputError

MAX_MEDIA_SECONDS = 7 * 24 * 60 * 60
MAX_STREAMS = 64
MAX_BOUNDARIES = 10_000
MAX_IMAGE_PIXELS = 200_000_000
MAX_IMAGE_FRAMES = 100
MAX_OBSERVATIONS = 1024
MAX_EMBEDDING_DIMENSION = 4096
MAX_TEXT_CHARACTERS = 4 * 1024**2
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DERIVED_OPERATIONS = {
    "extract_video_frame",
    "extract_normalized_audio_stream",
    "extract_faithful_audio_stream",
    "extract_normalized_audio_chunk",
    "extract_embedded_artwork",
    "extract_subtitle_stream",
}
_FACE_MODELS = {
    "yunet": ("face_detection_yunet_2023mar.onnx", "opencv-yunet-2023mar"),
    "sface": ("face_recognition_sface_2021dec.onnx", "opencv-sface-2021dec"),
    "expression": (
        "facial_expression_recognition_mobilefacenet_2022july.onnx",
        "opencv-fer-mobilefacenet-2022july",
    ),
    "age_attributes_xml": (
        "age-gender-recognition-retail-0013.xml",
        "openvino-age_attributes_xml",
    ),
    "age_attributes_bin": (
        "age-gender-recognition-retail-0013.bin",
        "openvino-age_attributes_bin",
    ),
    "head_pose_xml": ("head-pose-estimation-adas-0001.xml", "openvino-head_pose_xml"),
    "head_pose_bin": ("head-pose-estimation-adas-0001.bin", "openvino-head_pose_bin"),
}
_VOICE_MODELS = {
    "silero_vad": ("silero_vad_v4_9e2449e1.onnx", "k2-fsa-silero-vad-v4"),
    "campplus": (
        "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
        "3dspeaker-campp-zh-en-16k",
    ),
}


def bind_model_provenance(operation, value, models_root):
    """Replace child-selected provenance with trusted, locally verified bindings.

    Hashing the fixed public files is safe in the caller; decoding or inference
    remains isolated. This proves which qualified artifacts the operation was
    configured to use, not that a compromised native runtime honestly ran them.
    The function mutates the already-validated JSON value before reconstruction.
    """
    from pandect.processing.extractors.model_files import validate_local_model
    from pandect.processing.model_manifest import MODEL_ARTIFACTS

    if operation not in {"analyze_faces", "analyze_voice_file"}:
        raise InvalidExtractorOutputError
    face = operation == "analyze_faces"
    selected = _FACE_MODELS if face else _VOICE_MODELS
    manifest = {artifact.filename: artifact for artifact in MODEL_ARTIFACTS}
    models = {}
    for key, (filename, identifier) in selected.items():
        artifact = manifest[filename]
        checked = validate_local_model(
            Path(models_root) / filename,
            identifier=identifier,
            expected_sha256=artifact.sha256,
        )
        models[key] = {
            **checked.as_dict(),
            "filename": filename,
            "revision": artifact.revision,
        }
    preprocessing = "yunet-sface-alignment-v1" if face else "mono-16khz-float-v1"
    provenance = {
        "processor": "pandect.local-face-observation"
        if face
        else "pandect.local-voice-observation",
        "processor_version": "1",
        "models": models,
        "preprocessing_version": preprocessing,
        "inference_threads": 1,
        "runtime": {
            "name": "opencv+openvino" if face else "sherpa-onnx",
            "version": version("opencv-python-headless" if face else "sherpa-onnx"),
        },
        "model_attribution": "trusted-selected-and-hash-verified-artifacts",
        "result_trust": "untrusted-media-child-output",
        "training_corpus_limitation": (
            "The complete training-corpus composition is not disclosed for all models."
        ),
    }
    if not face:
        from pandect.processing.extractors.voice import (
            MUSIC_HEURISTIC_LIMITATION,
            MUSIC_HEURISTIC_REVISION,
            MUSIC_HEURISTIC_THRESHOLDS,
        )

        provenance["music_dominance_heuristic"] = {
            "method": "deterministic_signal_heuristic",
            "revision": MUSIC_HEURISTIC_REVISION,
            "thresholds": dict(MUSIC_HEURISTIC_THRESHOLDS),
            "limitation": MUSIC_HEURISTIC_LIMITATION,
        }
    feature_space = (
        f"opencv-sface-2021dec:{models['sface']['sha256']}:yunet-align-v1"
        if face
        else f"campp-zh-en-16k:{models['campplus']['sha256']}:mono16k-v1"
    )
    value["provenance"] = provenance
    for observation in value["observations"]:
        observation["provenance"] = deepcopy(provenance)
        observation["feature_space"] = feature_space


def _mapping(value):
    if not isinstance(value, dict):
        raise InvalidExtractorOutputError
    return value


def _sequence(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise InvalidExtractorOutputError
    return value


def _number(value, low=0, high=MAX_MEDIA_SECONDS, *, integer=False, optional=False):
    if value is None and optional:
        return None
    if type(value) not in ((int,) if integer else (int, float)):
        raise InvalidExtractorOutputError
    # Compare before isfinite so enormous JSON integers cannot overflow float.
    if not low <= value <= high or not math.isfinite(value):
        raise InvalidExtractorOutputError
    return value


def _optional_number(mapping, name, low=0, high=MAX_MEDIA_SECONDS, *, integer=False):
    return _number(mapping.get(name), low, high, integer=integer, optional=True)


def _text(value, maximum=MAX_TEXT_CHARACTERS):
    if not isinstance(value, str) or len(value) > maximum:
        raise InvalidExtractorOutputError


def _geometry(value, *, pixel_key=None):
    value = _mapping(value)
    width = _number(value.get("width"), 1, MAX_IMAGE_PIXELS, integer=True)
    height = _number(value.get("height"), 1, MAX_IMAGE_PIXELS, integer=True)
    pixels = width * height
    if pixels > MAX_IMAGE_PIXELS:
        raise InvalidExtractorOutputError
    if (
        pixel_key is not None
        and _number(value.get(pixel_key), 1, MAX_IMAGE_PIXELS, integer=True) != pixels
    ):
        raise InvalidExtractorOutputError
    return pixels


def _probe(value):
    raw = _mapping(value.get("raw"))
    _mapping(raw.get("format", {}))
    for name, limit in (("streams", MAX_STREAMS), ("chapters", MAX_BOUNDARIES)):
        for item in _sequence(raw.get(name, []), limit):
            _mapping(item)
    technical = _mapping(value.get("technical"))
    container = _mapping(technical.get("format"))
    _optional_number(container, "duration_seconds")
    _optional_number(container, "start_time_seconds", -MAX_MEDIA_SECONDS)
    _optional_number(container, "stream_count", high=MAX_STREAMS, integer=True)
    streams = _sequence(technical.get("streams"), MAX_STREAMS)
    indexes = set()
    for stream in streams:
        _mapping(stream)
        index = _number(stream.get("index"), 0, 65535, integer=True)
        if index in indexes:
            raise InvalidExtractorOutputError
        indexes.add(index)
        _optional_number(stream, "duration_seconds")
        _optional_number(stream, "start_time_seconds", -MAX_MEDIA_SECONDS)
        width = _optional_number(stream, "width", high=MAX_IMAGE_PIXELS, integer=True)
        height = _optional_number(stream, "height", high=MAX_IMAGE_PIXELS, integer=True)
        if width and height and width * height > MAX_IMAGE_PIXELS:
            raise InvalidExtractorOutputError
        _optional_number(stream, "channels", high=256, integer=True)
        _optional_number(stream, "sample_rate", high=1_536_000, integer=True)
        _mapping(stream.get("tags", {}))
        _mapping(stream.get("disposition", {}))
        if "codec_type" in stream and stream["codec_type"] is not None:
            _text(stream["codec_type"], 128)
        if "codec_name" in stream and stream["codec_name"] is not None:
            _text(stream["codec_name"], 128)
    for chapter in _sequence(technical.get("chapters"), MAX_BOUNDARIES):
        _mapping(chapter)
        start = _optional_number(chapter, "start_time_seconds")
        end = _optional_number(chapter, "end_time_seconds")
        if start is not None and end is not None and end < start:
            raise InvalidExtractorOutputError
        _mapping(chapter.get("tags", {}))


def _image(value):
    technical = _mapping(value.get("technical"))
    _geometry(technical, pixel_key="pixel_count")
    total = _number(technical.get("frame_count"), 1, 2**31 - 1, integer=True)
    frames = _sequence(technical.get("frames"), MAX_IMAGE_FRAMES)
    count = _number(technical.get("frames_analyzed"), 1, MAX_IMAGE_FRAMES, integer=True)
    if count != len(frames) or count > total:
        raise InvalidExtractorOutputError
    decoded_pixels = 0
    for expected_index, frame in enumerate(frames):
        decoded_pixels += _geometry(frame)
        if type(frame.get("index")) is not int or frame["index"] != expected_index:
            raise InvalidExtractorOutputError
    if (
        _number(technical.get("decoded_pixel_count"), 1, MAX_IMAGE_PIXELS, integer=True)
        != decoded_pixels
    ):
        raise InvalidExtractorOutputError


def _ocr(value):
    _text(value.get("text"))
    _number(value.get("page_count"), 0, MAX_IMAGE_FRAMES, integer=True)
    _optional_number(value, "mean_confidence", 0, 100)
    for name in ("words", "lines", "blocks"):
        for item in _sequence(value.get(name), 20_000):
            _mapping(item)
            _text(item.get("text"))
            _number(item.get("page_num"), 0, MAX_IMAGE_FRAMES, integer=True)
            geometry = item if name == "words" else _mapping(item.get("box"))
            for coordinate in ("left", "top", "width", "height"):
                _number(geometry.get(coordinate), 0, MAX_IMAGE_PIXELS, integer=True)
            if name == "words":
                _optional_number(item, "confidence", -1, 100)


def _embedding(value, *, optional=False):
    if value is None and optional:
        return
    if not _sequence(value, MAX_EMBEDDING_DIMENSION):
        raise InvalidExtractorOutputError
    for number in value:
        _number(number, -1, 1)
    magnitude = math.fsum(number * number for number in value)
    if not 0.99 <= magnitude <= 1.01:
        raise InvalidExtractorOutputError


def _observations(value, *, voice=False):
    _mapping(value.get("provenance"))
    for warning in _sequence(value.get("warnings"), MAX_OBSERVATIONS):
        _text(warning, 1024)
    observations = _sequence(value.get("observations"), MAX_OBSERVATIONS)
    for observation in observations:
        _mapping(observation)
        _text(observation.get("unit_key"), 255)
        _text(observation.get("feature_space"), 255)
        _number(observation.get("quality_score"), 0, 1)
        _mapping(observation.get("provenance"))
        _mapping(observation.get("quality_details"))
        _embedding(observation.get("embedding"), optional=voice)
        if voice:
            _voice_interval(observation)
        else:
            _number(observation.get("detector_confidence"), 0, 1)
            box = _sequence(observation.get("bbox"), 4)
            if len(box) != 4:
                raise InvalidExtractorOutputError
            for position, coordinate in enumerate(box):
                _number(coordinate, 0 if position < 2 else 1e-9, MAX_IMAGE_PIXELS)
            if box[2] * box[3] > MAX_IMAGE_PIXELS:
                raise InvalidExtractorOutputError
    if voice:
        for name in ("speech_regions", "non_speech_regions"):
            for region in _sequence(value.get(name), MAX_OBSERVATIONS):
                _voice_interval(_mapping(region))


def _voice_interval(value):
    start = _number(value.get("start_ms"), 0, MAX_MEDIA_SECONDS * 1000, integer=True)
    end = _number(value.get("end_ms"), 0, MAX_MEDIA_SECONDS * 1000, integer=True)
    if not 0 < end - start <= 601_000:
        raise InvalidExtractorOutputError


def _fingerprint(value):
    if not isinstance(value.get("sha256"), str) or not _SHA256.fullmatch(
        value["sha256"]
    ):
        raise InvalidExtractorOutputError
    _number(value.get("size_bytes"), 0, 1024**3, integer=True)


def validate_operation_result(operation, value):
    """Reject malformed or amplifying results before typed reconstruction.

    ``value`` is the operation's raw JSON value (not the transport envelope).
    QR and diagnostic operations pass their result object directly instead.
    Annotation checks still run afterward; this supplements rather than replaces
    the strict recursive type decoder and protocol size/depth/node limits.
    """
    if value is None and operation == "extract_subtitle_stream":
        return
    if operation in {"detect_silence", "detect_scene_changes"}:
        for entry in _sequence(value, MAX_BOUNDARIES):
            _mapping(entry)
            if operation == "detect_silence":
                start = _number(entry.get("start_seconds"))
                end = _number(entry.get("end_seconds"))
                if end < start:
                    raise InvalidExtractorOutputError
            else:
                _number(entry.get("timestamp_seconds"))
                _number(entry.get("score"), 0, 1)
        return
    value = _mapping(value)
    if operation == "probe_media":
        _probe(value)
    elif operation == "analyze_image":
        _image(value)
    elif operation == "render_image_frame":
        _geometry(value, pixel_key="pixel_count")
        _number(value.get("frame_index"), 0, MAX_IMAGE_FRAMES - 1, integer=True)
    elif operation == "extract_ocr":
        _ocr(value)
    elif operation == "extract_metadata":
        _mapping(value.get("raw_tags"))
        if value.get("gps") is not None:
            gps = _mapping(value["gps"])
            _optional_number(gps, "latitude", -90, 90)
            _optional_number(gps, "longitude", -180, 180)
            _optional_number(gps, "altitude_metres", -20_000, 100_000)
    elif operation in {"analyze_faces", "analyze_voice_file"}:
        _observations(value, voice=operation == "analyze_voice_file")
    elif operation == "extract_audio_chunk" or operation in _DERIVED_OPERATIONS:
        _fingerprint(value)
        if operation in _DERIVED_OPERATIONS:
            _optional_number(value, "stream_index", high=65535, integer=True)
            _optional_number(value, "frame_index", high=2**31 - 1, integer=True)
            start = _optional_number(value, "source_start_seconds")
            end = _optional_number(value, "source_end_seconds")
            if start is not None and end is not None and end < start:
                raise InvalidExtractorOutputError
    elif operation == "analyze_acoustics":
        _mapping(value.get("loudness"))
        _mapping(value.get("volume"))
    elif operation == "decode_qr":
        for code in _sequence(value.get("codes"), MAX_OBSERVATIONS):
            _mapping(code)
            _text(code.get("value"))
            for point in _sequence(code.get("polygon"), 4):
                if len(_sequence(point, 2)) != 2:
                    raise InvalidExtractorOutputError
                for coordinate in point:
                    _number(coordinate, 0, MAX_IMAGE_PIXELS)
    elif operation == "tool_version":
        _text(value.get("version"), 160)
        if not value["version"] or not value["version"].isprintable():
            raise InvalidExtractorOutputError
    elif operation == "model_smoke":
        for check in _sequence(value.get("checks"), 32):
            _mapping(check)
    else:
        raise InvalidExtractorOutputError

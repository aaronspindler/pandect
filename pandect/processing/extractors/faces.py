"""Offline face observations and opaque SFace features.

The public result deliberately omits embeddings. Callers that persist local
closed-gallery features must read :attr:`FaceObservation.embedding` directly;
``repr()`` and ``as_dict()`` never expose it.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Any, Protocol

from pandect.media_runner.routing import isolated, require_child
from pandect.processing.extractors.errors import (
    InvalidExtractorOutputError,
    ModelIntegrityError,
    OptionalDependencyUnavailableError,
    ToolFailedError,
    UnsupportedPixelFormatError,
)
from pandect.processing.extractors.model_files import (
    LocalModelProvenance,
    validate_local_model,
)

YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
EXPRESSION_SHA256 = "4f61307602fc089ce20488a31d4e4614e3c9753a7d6c41578c854858b183e1a9"
AGE_ATTRIBUTES_XML_SHA256 = (
    "5452aa9372d38f92e54af7f9a2b23e61f5a3bcb90ed17bc90b50111784dd574b"
)
AGE_ATTRIBUTES_BIN_SHA256 = (
    "6f36b6ec9020bf26151e2ec0e9d4d0848a0694935a681ff54e60c01273e931f3"
)
HEAD_POSE_XML_SHA256 = (
    "bea4b6428168093ac827a544ddf1d96894106811d8c481bc372aa555b0e8450a"
)
HEAD_POSE_BIN_SHA256 = (
    "fd7e39c5f115a0fddf192b471a796d55342907a164580d78911c8716a3645056"
)

EXPRESSION_LABELS = (
    "angry",
    "disgust",
    "fearful",
    "happy",
    "neutral",
    "sad",
    "surprised",
)
LANDMARK_LABELS = (
    "right_eye",
    "left_eye",
    "nose_tip",
    "right_mouth_corner",
    "left_mouth_corner",
)


@dataclass(frozen=True)
class FaceModelPaths:
    """Explicit, pre-provisioned model paths. No loader downloads weights."""

    yunet: Path
    sface: Path
    expression: Path
    age_attributes_xml: Path | None = None
    age_attributes_bin: Path | None = None
    head_pose_xml: Path | None = None
    head_pose_bin: Path | None = None

    @classmethod
    def from_root(cls, root: Path) -> "FaceModelPaths":
        return cls(
            yunet=root / "face_detection_yunet_2023mar.onnx",
            sface=root / "face_recognition_sface_2021dec.onnx",
            expression=(
                root / "facial_expression_recognition_mobilefacenet_2022july.onnx"
            ),
            age_attributes_xml=root / "age-gender-recognition-retail-0013.xml",
            age_attributes_bin=root / "age-gender-recognition-retail-0013.bin",
            head_pose_xml=root / "head-pose-estimation-adas-0001.xml",
            head_pose_bin=root / "head-pose-estimation-adas-0001.bin",
        )


DEFAULT_FACE_HASHES: Mapping[str, str] = {
    "yunet": YUNET_SHA256,
    "sface": SFACE_SHA256,
    "expression": EXPRESSION_SHA256,
    "age_attributes_xml": AGE_ATTRIBUTES_XML_SHA256,
    "age_attributes_bin": AGE_ATTRIBUTES_BIN_SHA256,
    "head_pose_xml": HEAD_POSE_XML_SHA256,
    "head_pose_bin": HEAD_POSE_BIN_SHA256,
}


@dataclass(frozen=True)
class ImageFrame:
    width: int
    height: int
    data: Any = field(repr=False)


@dataclass(frozen=True)
class DetectedFace:
    bbox: tuple[float, float, float, float]
    landmarks: tuple[tuple[float, float], ...]
    confidence: float
    token: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class AlignedFace:
    width: int
    height: int
    brightness_mean: float
    sharpness_variance: float
    data: Any = field(repr=False)


@dataclass(frozen=True)
class ApparentAttributes:
    apparent_age: float | None
    binary_visual_class_scores: tuple[float, float] | None


@dataclass(frozen=True)
class HeadPose:
    yaw: float
    pitch: float
    roll: float


class FaceInferenceRuntime(Protocol):
    """Narrow injection boundary used by real and fake inference runtimes."""

    name: str
    version: str

    def load_image(self, path: str | PathLike[str]) -> ImageFrame: ...

    def detect(self, frame: ImageFrame) -> Sequence[DetectedFace]: ...

    def align(self, frame: ImageFrame, detection: DetectedFace) -> AlignedFace: ...

    def embedding(self, aligned: AlignedFace) -> Sequence[float]: ...

    def expression_logits(self, aligned: AlignedFace) -> Sequence[float]: ...

    def apparent_attributes(
        self, aligned: AlignedFace
    ) -> ApparentAttributes | None: ...

    def head_pose(self, aligned: AlignedFace) -> HeadPose | None: ...


@dataclass(frozen=True)
class FaceObservation:
    unit_key: str
    bbox: tuple[float, float, float, float]
    landmarks: Mapping[str, tuple[float, float]]
    detector_confidence: float
    quality_score: float
    quality_assessment: str
    quality_details: Mapping[str, float | str]
    apparent_facial_expression_scores: Mapping[str, float]
    apparent_age_band: str | None
    binary_visual_class_scores: Mapping[str, float] | None
    head_pose_degrees: Mapping[str, float] | None
    feature_space: str
    provenance: Mapping[str, Any]
    embedding: tuple[float, ...] = field(repr=False)

    def as_dict(self) -> dict[str, Any]:
        """Return the complete display-safe observation, never its embedding."""
        return {
            "unit_key": self.unit_key,
            "bbox": list(self.bbox),
            "landmarks": {
                label: list(coordinates)
                for label, coordinates in self.landmarks.items()
            },
            "detector_confidence": self.detector_confidence,
            "quality_score": self.quality_score,
            "quality_assessment": self.quality_assessment,
            "quality_details": dict(self.quality_details),
            "apparent_facial_expression_scores": dict(
                self.apparent_facial_expression_scores
            ),
            "apparent_age_band": self.apparent_age_band,
            "binary_visual_class_scores": (
                dict(self.binary_visual_class_scores)
                if self.binary_visual_class_scores is not None
                else None
            ),
            "head_pose_degrees": (
                dict(self.head_pose_degrees)
                if self.head_pose_degrees is not None
                else None
            ),
            "embedding_dimension": len(self.embedding),
            "feature_space": self.feature_space,
            "provenance": dict(self.provenance),
            "limitations": {
                "expression": (
                    "Apparent facial expression scores are not emotion, intent, "
                    "or truth."
                ),
                "age": "Apparent age is shown only as a broad adult-range band.",
                "binary_visual_classes": (
                    "Model output classes are visual scores, not sex or gender "
                    "identity."
                ),
            },
        }


@dataclass(frozen=True)
class FaceAnalysis:
    observations: tuple[FaceObservation, ...]
    warnings: tuple[str, ...]
    provenance: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "observations": [value.as_dict() for value in self.observations],
            "warnings": list(self.warnings),
            "provenance": dict(self.provenance),
        }


def _validate_models(
    paths: FaceModelPaths,
    expected_hashes: Mapping[str, str],
) -> dict[str, LocalModelProvenance]:
    configured: dict[str, tuple[Path | None, str]] = {
        "yunet": (paths.yunet, "opencv-yunet-2023mar"),
        "sface": (paths.sface, "opencv-sface-2021dec"),
        "expression": (paths.expression, "opencv-fer-mobilefacenet-2022july"),
    }
    optional_pairs = (
        (
            "age_attributes_xml",
            paths.age_attributes_xml,
            "age_attributes_bin",
            paths.age_attributes_bin,
        ),
        (
            "head_pose_xml",
            paths.head_pose_xml,
            "head_pose_bin",
            paths.head_pose_bin,
        ),
    )
    for xml_name, xml_path, bin_name, bin_path in optional_pairs:
        xml_present = xml_path is not None and xml_path.is_file()
        bin_present = bin_path is not None and bin_path.is_file()
        hashes_configured = xml_name in expected_hashes or bin_name in expected_hashes
        if not xml_present and not bin_present and not hashes_configured:
            continue
        if not xml_present or not bin_present:
            raise ModelIntegrityError
        configured[xml_name] = (xml_path, f"openvino-{xml_name}")
        configured[bin_name] = (bin_path, f"openvino-{bin_name}")

    checks: dict[str, LocalModelProvenance] = {}
    for key, (path, identifier) in configured.items():
        expected = expected_hashes.get(key)
        if path is None or expected is None:
            raise ModelIntegrityError
        checks[key] = validate_local_model(
            path,
            identifier=identifier,
            expected_sha256=expected,
        )
    return checks


def _finite_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise InvalidExtractorOutputError from error
    if not math.isfinite(result):
        raise InvalidExtractorOutputError
    return result


def _normalize_embedding(values: Sequence[float]) -> tuple[float, ...]:
    vector = tuple(_finite_float(value) for value in values)
    if not vector:
        raise InvalidExtractorOutputError
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude <= 1e-12:
        raise InvalidExtractorOutputError
    return tuple(value / magnitude for value in vector)


def _probabilities(values: Sequence[float], *, expected: int) -> tuple[float, ...]:
    raw = tuple(_finite_float(value) for value in values)
    if len(raw) != expected:
        raise InvalidExtractorOutputError
    total = sum(raw)
    if all(value >= 0 for value in raw) and 0.98 <= total <= 1.02:
        return tuple(value / total for value in raw)
    maximum = max(raw)
    exponentials = tuple(math.exp(value - maximum) for value in raw)
    denominator = sum(exponentials)
    return tuple(value / denominator for value in exponentials)


def _apparent_age_band(value: float | None) -> str | None:
    if value is None:
        return None
    age = _finite_float(value)
    if not 18 <= age <= 75:
        return None
    if age < 30:
        return "18-29"
    if age < 45:
        return "30-44"
    if age < 60:
        return "45-59"
    return "60-75"


def _clamped_bbox(
    bbox: Sequence[float], width: int, height: int
) -> tuple[float, float, float, float]:
    if len(bbox) != 4 or width <= 0 or height <= 0:
        raise InvalidExtractorOutputError
    x, y, box_width, box_height = (_finite_float(value) for value in bbox)
    left = min(max(x, 0.0), float(width))
    top = min(max(y, 0.0), float(height))
    right = min(max(x + box_width, 0.0), float(width))
    bottom = min(max(y + box_height, 0.0), float(height))
    if right <= left or bottom <= top:
        raise InvalidExtractorOutputError
    return (left, top, right - left, bottom - top)


def _quality(
    detection: DetectedFace,
    bbox: tuple[float, float, float, float],
    aligned: AlignedFace,
) -> tuple[float, str, dict[str, float | str]]:
    confidence = min(max(_finite_float(detection.confidence), 0.0), 1.0)
    minimum_dimension = min(bbox[2], bbox[3])
    size_score = min(1.0, minimum_dimension / 112.0)
    sharpness = max(0.0, _finite_float(aligned.sharpness_variance))
    sharpness_score = min(1.0, math.log1p(sharpness) / math.log1p(500.0))
    brightness = min(max(_finite_float(aligned.brightness_mean), 0.0), 255.0)
    exposure_score = max(0.0, 1.0 - abs(brightness - 127.5) / 127.5)
    score = round(
        0.4 * confidence
        + 0.25 * size_score
        + 0.2 * sharpness_score
        + 0.15 * exposure_score,
        6,
    )
    assessment = "usable" if score >= 0.6 and minimum_dimension >= 48 else "degraded"
    return (
        score,
        assessment,
        {
            "detector_confidence": round(confidence, 6),
            "minimum_face_dimension_pixels": round(minimum_dimension, 3),
            "size_score": round(size_score, 6),
            "sharpness_variance": round(sharpness, 6),
            "sharpness_score": round(sharpness_score, 6),
            "brightness_mean": round(brightness, 6),
            "exposure_score": round(exposure_score, 6),
        },
    )


@isolated("analyze_faces", source="image_path")
def analyze_faces(
    image_path: str | PathLike[str],
    *,
    model_paths: FaceModelPaths,
    expected_hashes: Mapping[str, str] = DEFAULT_FACE_HASHES,
    runtime: FaceInferenceRuntime | None = None,
) -> FaceAnalysis:
    """Analyze locally detected faces using only hash-bound model files."""
    model_checks = _validate_models(model_paths, expected_hashes)
    inference = runtime or OpenCVFaceRuntime(model_paths)
    frame = inference.load_image(image_path)
    if frame.width <= 0 or frame.height <= 0:
        raise InvalidExtractorOutputError

    model_provenance = {
        key: value.as_dict() for key, value in sorted(model_checks.items())
    }
    provenance = {
        "processor": "pandect.local-face-observation",
        "processor_version": "1",
        "runtime": {"name": inference.name, "version": inference.version},
        "models": model_provenance,
        "preprocessing_version": "yunet-sface-alignment-v1",
        "inference_threads": 1,
        "training_corpus_limitation": (
            "The complete training-corpus composition is not disclosed for all models."
        ),
    }
    observations: list[FaceObservation] = []
    warnings: list[str] = []
    for face_index, detection in enumerate(inference.detect(frame), start=1):
        try:
            bbox = _clamped_bbox(detection.bbox, frame.width, frame.height)
            if len(detection.landmarks) != len(LANDMARK_LABELS):
                raise InvalidExtractorOutputError
            landmarks = {
                label: (_finite_float(point[0]), _finite_float(point[1]))
                for label, point in zip(
                    LANDMARK_LABELS, detection.landmarks, strict=True
                )
                if len(point) == 2
            }
            if len(landmarks) != len(LANDMARK_LABELS):
                raise InvalidExtractorOutputError
            aligned = inference.align(frame, detection)
            embedding = _normalize_embedding(inference.embedding(aligned))
            expression = _probabilities(
                inference.expression_logits(aligned), expected=len(EXPRESSION_LABELS)
            )
            expression_scores = {
                label: round(score, 8)
                for label, score in zip(EXPRESSION_LABELS, expression, strict=True)
            }
            attributes = inference.apparent_attributes(aligned)
            age_band = None
            binary_scores = None
            if attributes is not None:
                age_band = _apparent_age_band(attributes.apparent_age)
                if attributes.binary_visual_class_scores is not None:
                    values = _probabilities(
                        attributes.binary_visual_class_scores, expected=2
                    )
                    binary_scores = {
                        "class_0": round(values[0], 8),
                        "class_1": round(values[1], 8),
                    }
            raw_pose = inference.head_pose(aligned)
            pose = None
            if raw_pose is not None:
                pose = {
                    "yaw": round(_finite_float(raw_pose.yaw), 6),
                    "pitch": round(_finite_float(raw_pose.pitch), 6),
                    "roll": round(_finite_float(raw_pose.roll), 6),
                }
            quality_score, assessment, details = _quality(detection, bbox, aligned)
        except (InvalidExtractorOutputError, IndexError, TypeError):
            warnings.append(f"face:{face_index:03d}:invalid_model_output")
            continue

        observations.append(
            FaceObservation(
                unit_key=f"face:{face_index:03d}",
                bbox=bbox,
                landmarks=landmarks,
                detector_confidence=round(_finite_float(detection.confidence), 8),
                quality_score=quality_score,
                quality_assessment=assessment,
                quality_details=details,
                apparent_facial_expression_scores=expression_scores,
                apparent_age_band=age_band,
                binary_visual_class_scores=binary_scores,
                head_pose_degrees=pose,
                feature_space=(
                    f"opencv-sface-2021dec:{model_checks['sface'].sha256}:"
                    "yunet-align-v1"
                ),
                provenance=provenance,
                embedding=embedding,
            )
        )
    return FaceAnalysis(tuple(observations), tuple(warnings), provenance)


class OpenCVFaceRuntime:
    """Pinned OpenCV/OpenVINO runtime with one native inference thread."""

    name = "opencv+openvino"

    def __init__(self, paths: FaceModelPaths):
        require_child()
        try:
            import cv2
            import numpy as np
        except ImportError as error:  # pragma: no cover - runtime image concern.
            raise OptionalDependencyUnavailableError from error
        self._cv2 = cv2
        self._np = np
        self.version = str(cv2.__version__)
        cv2.setNumThreads(1)
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(paths.yunet), "", (320, 320), 0.7, 0.3, 5000
            )
            self._recognizer = cv2.FaceRecognizerSF.create(str(paths.sface), "")
            self._expression = cv2.dnn.readNet(str(paths.expression))
            self._expression.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self._expression.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self._age = self._compile_openvino(
                paths.age_attributes_xml, paths.age_attributes_bin
            )
            self._pose = self._compile_openvino(
                paths.head_pose_xml, paths.head_pose_bin
            )
        except (OSError, RuntimeError, cv2.error) as error:
            raise ToolFailedError from error

    def _compile_openvino(self, xml_path: Path | None, bin_path: Path | None):
        if xml_path is None or bin_path is None:
            return None
        try:
            from openvino import Core
        except ImportError as error:  # pragma: no cover - runtime image concern.
            raise OptionalDependencyUnavailableError from error
        try:
            core = Core()
            model = core.read_model(model=str(xml_path), weights=str(bin_path))
            return core.compile_model(
                model,
                "CPU",
                {"INFERENCE_NUM_THREADS": 1, "PERFORMANCE_HINT": "LATENCY"},
            )
        except RuntimeError as error:
            raise ToolFailedError from error

    def load_image(self, path: str | PathLike[str]) -> ImageFrame:
        try:
            value = self._cv2.imread(str(path), self._cv2.IMREAD_COLOR)
        except (OSError, RuntimeError, self._cv2.error) as error:
            raise ToolFailedError from error
        if value is None or getattr(value, "ndim", 0) != 3:
            # The caller performs a bounded Pillow decode before face
            # inference. A source that reaches this point is not merely a
            # corrupt/unknown image; OpenCV cannot consume its pixel format.
            raise UnsupportedPixelFormatError
        height, width = value.shape[:2]
        return ImageFrame(int(width), int(height), value)

    def detect(self, frame: ImageFrame) -> Sequence[DetectedFace]:
        try:
            self._detector.setInputSize((frame.width, frame.height))
            _, values = self._detector.detect(frame.data)
        except (RuntimeError, self._cv2.error) as error:
            raise ToolFailedError from error
        if values is None:
            return ()
        result = []
        for row in values:
            flattened = row.reshape(-1)
            if len(flattened) < 15:
                continue
            landmarks = tuple(
                (float(flattened[index]), float(flattened[index + 1]))
                for index in range(4, 14, 2)
            )
            result.append(
                DetectedFace(
                    bbox=tuple(float(value) for value in flattened[:4]),
                    landmarks=landmarks,
                    confidence=float(flattened[14]),
                    token=row,
                )
            )
        return tuple(result)

    def align(self, frame: ImageFrame, detection: DetectedFace) -> AlignedFace:
        try:
            aligned = self._recognizer.alignCrop(frame.data, detection.token)
            grayscale = self._cv2.cvtColor(aligned, self._cv2.COLOR_BGR2GRAY)
            sharpness = float(self._cv2.Laplacian(grayscale, self._cv2.CV_64F).var())
            brightness = float(grayscale.mean())
        except (RuntimeError, self._cv2.error) as error:
            raise ToolFailedError from error
        height, width = aligned.shape[:2]
        return AlignedFace(width, height, brightness, sharpness, aligned)

    def embedding(self, aligned: AlignedFace) -> Sequence[float]:
        try:
            return self._recognizer.feature(aligned.data).reshape(-1).tolist()
        except (RuntimeError, self._cv2.error) as error:
            raise ToolFailedError from error

    def expression_logits(self, aligned: AlignedFace) -> Sequence[float]:
        try:
            blob = self._cv2.dnn.blobFromImage(
                aligned.data,
                scalefactor=1 / 127.5,
                size=(112, 112),
                mean=(127.5, 127.5, 127.5),
                swapRB=True,
                crop=False,
            )
            self._expression.setInput(blob)
            return self._expression.forward().reshape(-1).tolist()
        except (RuntimeError, self._cv2.error) as error:
            raise ToolFailedError from error

    def _openvino_outputs(self, compiled, aligned: AlignedFace) -> dict[str, Any]:
        input_shape = tuple(int(value) for value in compiled.input(0).shape)
        if len(input_shape) != 4:
            raise ToolFailedError
        image = self._cv2.resize(aligned.data, (input_shape[3], input_shape[2]))
        blob = self._np.expand_dims(image.transpose(2, 0, 1), 0).astype("float32")
        try:
            inferred = compiled([blob])
        except RuntimeError as error:
            raise ToolFailedError from error
        outputs = {}
        for output in compiled.outputs:
            try:
                name = output.any_name
            except RuntimeError:
                names = sorted(output.get_names())
                name = names[0] if names else str(output.index)
            outputs[name.casefold()] = inferred[output]
        return outputs

    def apparent_attributes(self, aligned: AlignedFace) -> ApparentAttributes | None:
        if self._age is None:
            return None
        values = self._openvino_outputs(self._age, aligned)
        age_output = next(
            (value for name, value in values.items() if "age" in name), None
        )
        class_output = next(
            (value for name, value in values.items() if "prob" in name), None
        )
        if age_output is None or class_output is None:
            raise ToolFailedError
        apparent_age = float(self._np.asarray(age_output).reshape(-1)[0]) * 100
        scores = self._np.asarray(class_output).reshape(-1)
        if len(scores) < 2:
            raise ToolFailedError
        return ApparentAttributes(apparent_age, (float(scores[0]), float(scores[1])))

    def head_pose(self, aligned: AlignedFace) -> HeadPose | None:
        if self._pose is None:
            return None
        values = self._openvino_outputs(self._pose, aligned)

        def angle(marker: str) -> float:
            output = next(
                (value for name, value in values.items() if marker in name), None
            )
            if output is None:
                raise ToolFailedError
            return float(self._np.asarray(output).reshape(-1)[0])

        return HeadPose(
            yaw=angle("angle_y"), pitch=angle("angle_p"), roll=angle("angle_r")
        )

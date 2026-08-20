import math
import random
from dataclasses import dataclass
from hashlib import sha256

import pytest

from pandect.processing.extractors.errors import (
    ModelIntegrityError,
    ModelUnavailableError,
)
from pandect.processing.extractors.faces import (
    AlignedFace,
    ApparentAttributes,
    DetectedFace,
    FaceModelPaths,
    HeadPose,
    ImageFrame,
    analyze_faces,
)
from pandect.processing.extractors.voice import (
    SAMPLE_RATE,
    VadSegment,
    VoiceModelPaths,
    analyze_voice,
)


def _model(path, value=b"model"):
    path.write_bytes(value)
    return sha256(value).hexdigest()


@dataclass
class FakeFaceRuntime:
    name = "fake-face-runtime"
    version = "test-1"

    def load_image(self, path):
        return ImageFrame(320, 240, object())

    def detect(self, frame):
        return (
            DetectedFace(
                (40, 20, 120, 120),
                ((70, 60), (120, 60), (95, 85), (75, 115), (115, 115)),
                0.95,
            ),
        )

    def align(self, frame, detection):
        return AlignedFace(112, 112, 125, 300, object())

    def embedding(self, aligned):
        return (3.0, 4.0)

    def expression_logits(self, aligned):
        return (0.05, 0.05, 0.05, 0.5, 0.2, 0.1, 0.05)

    def apparent_attributes(self, aligned):
        return ApparentAttributes(37, (0.4, 0.6))

    def head_pose(self, aligned):
        return HeadPose(4, -2, 1)


def _face_models(tmp_path):
    paths = FaceModelPaths(
        yunet=tmp_path / "yunet.onnx",
        sface=tmp_path / "sface.onnx",
        expression=tmp_path / "fer.onnx",
    )
    hashes = {
        "yunet": _model(paths.yunet, b"yunet"),
        "sface": _model(paths.sface, b"sface"),
        "expression": _model(paths.expression, b"fer"),
    }
    return paths, hashes


def test_face_analysis_returns_labeled_hypotheses_and_keeps_embedding_opaque(
    tmp_path,
):
    paths, hashes = _face_models(tmp_path)

    result = analyze_faces(
        tmp_path / "evidence.jpg",
        model_paths=paths,
        expected_hashes=hashes,
        runtime=FakeFaceRuntime(),
    )

    observation = result.observations[0]
    public = result.as_dict()["observations"][0]
    assert observation.embedding == pytest.approx((0.6, 0.8))
    assert "embedding" not in public
    assert "embedding" not in repr(observation)
    assert public["embedding_dimension"] == 2
    assert set(public["apparent_facial_expression_scores"]) == {
        "angry",
        "disgust",
        "fearful",
        "happy",
        "neutral",
        "sad",
        "surprised",
    }
    assert sum(public["apparent_facial_expression_scores"].values()) == pytest.approx(1)
    assert public["apparent_age_band"] == "30-44"
    assert public["binary_visual_class_scores"] == {
        "class_0": 0.4,
        "class_1": 0.6,
    }
    assert public["head_pose_degrees"] == {"yaw": 4.0, "pitch": -2.0, "roll": 1.0}
    assert public["quality_assessment"] == "usable"
    assert public["provenance"]["inference_threads"] == 1


def test_face_analysis_withholds_apparent_age_outside_documented_adult_range(
    tmp_path,
):
    paths, hashes = _face_models(tmp_path)
    runtime = FakeFaceRuntime()
    runtime.apparent_attributes = lambda _aligned: ApparentAttributes(12, (0.2, 0.8))

    result = analyze_faces(
        tmp_path / "evidence.jpg",
        model_paths=paths,
        expected_hashes=hashes,
        runtime=runtime,
    )

    assert result.as_dict()["observations"][0]["apparent_age_band"] is None


def test_face_analysis_fails_safely_for_missing_or_modified_models(tmp_path):
    paths, hashes = _face_models(tmp_path)
    paths.sface.unlink()
    with pytest.raises(ModelUnavailableError) as missing:
        analyze_faces(
            tmp_path / "evidence.jpg",
            model_paths=paths,
            expected_hashes=hashes,
            runtime=FakeFaceRuntime(),
        )
    assert str(missing.value) == "A required local inference model is unavailable."
    assert str(paths.sface) not in str(missing.value)

    paths.sface.write_bytes(b"changed")
    with pytest.raises(ModelIntegrityError):
        analyze_faces(
            tmp_path / "evidence.jpg",
            model_paths=paths,
            expected_hashes=hashes,
            runtime=FakeFaceRuntime(),
        )


class FakeVoiceRuntime:
    name = "fake-voice-runtime"
    version = "test-1"

    def speech_segments(self, samples, sample_rate):
        return (
            VadSegment(0, 3 * sample_rate),
            VadSegment(4 * sample_rate, 5 * sample_rate),
        )

    def embedding(self, samples, sample_rate):
        return (3.0, 4.0)


class FullWindowVoiceRuntime(FakeVoiceRuntime):
    def speech_segments(self, samples, sample_rate):
        return (VadSegment(0, len(samples)),)


def _sine_wave(frequency_hz, *, seconds=3.0, amplitude=0.1):
    return [
        amplitude * math.sin(2 * math.pi * frequency_hz * index / SAMPLE_RATE)
        for index in range(round(seconds * SAMPLE_RATE))
    ]


def _noisy_tone(*, seconds=3.0, tone_amplitude=0.1, noise_amplitude=0.06):
    generator = random.Random(1729)
    return [
        tone_amplitude * math.sin(2 * math.pi * 440 * index / SAMPLE_RATE)
        + generator.uniform(-noise_amplitude, noise_amplitude)
        for index in range(round(seconds * SAMPLE_RATE))
    ]


def _speech_like_signal(*, seconds=3.0):
    """Deterministic, non-linguistic voiced signal with speech-like variation."""
    phase = 0.0
    samples = []
    for index in range(round(seconds * SAMPLE_RATE)):
        time_seconds = index / SAMPLE_RATE
        fundamental_hz = (
            135
            + 28 * math.sin(2 * math.pi * 1.7 * time_seconds)
            + 13 * math.sin(2 * math.pi * 3.1 * time_seconds)
        )
        phase += 2 * math.pi * fundamental_hz / SAMPLE_RATE
        envelope = 0.055 + 0.035 * (
            0.5 + 0.5 * math.sin(2 * math.pi * 2.3 * time_seconds)
        )
        samples.append(
            envelope
            * (
                math.sin(phase)
                + 0.35 * math.sin(2 * phase + 0.2)
                + 0.18 * math.sin(3 * phase + 0.5)
            )
        )
    return samples


def _broadband_noise(*, seconds=3.0, amplitude=0.1):
    generator = random.Random(1729)
    return [
        generator.uniform(-amplitude, amplitude)
        for _ in range(round(seconds * SAMPLE_RATE))
    ]


def _voice_models(tmp_path):
    paths = VoiceModelPaths(
        silero_vad=tmp_path / "silero.onnx",
        campplus=tmp_path / "campplus.onnx",
    )
    hashes = {
        "silero_vad": _model(paths.silero_vad, b"silero"),
        "campplus": _model(paths.campplus, b"campplus"),
    }
    return paths, hashes


def test_voice_analysis_applies_quality_gates_and_keeps_features_opaque(tmp_path):
    paths, hashes = _voice_models(tmp_path)
    waveform = [0.1] * (3 * SAMPLE_RATE) + [0.0] * SAMPLE_RATE + [0.1] * SAMPLE_RATE

    result = analyze_voice(
        waveform,
        model_paths=paths,
        expected_hashes=hashes,
        runtime=FakeVoiceRuntime(),
        source_start_ms=10_000,
    )

    first, second = result.observations
    first_public = result.as_dict()["observations"][0]
    assert first.start_ms == 10_000
    assert first.end_ms == 13_000
    assert first.embedding == pytest.approx((0.6, 0.8))
    assert "embedding" not in first_public
    assert "embedding=(" not in repr(first)
    assert first_public["overlap_status"] == "not_assessed"
    assert first_public["quality_details"]["music_dominance"] == "not_assessed"
    assert second.quality_assessment == "rejected"
    assert "too_short" in second.quality_reasons
    assert second.embedding is None
    assert result.speech_regions == (
        {"start_ms": 10_000, "end_ms": 13_000},
        {"start_ms": 14_000, "end_ms": 15_000},
    )
    assert result.non_speech_regions == ({"start_ms": 13_000, "end_ms": 14_000},)


def test_voice_analysis_rejects_clipped_window_without_claiming_music(tmp_path):
    paths, hashes = _voice_models(tmp_path)
    runtime = FakeVoiceRuntime()
    runtime.speech_segments = lambda _samples, sample_rate: (
        VadSegment(0, 2 * sample_rate),
    )

    result = analyze_voice(
        [1.0] * (2 * SAMPLE_RATE),
        model_paths=paths,
        expected_hashes=hashes,
        runtime=runtime,
    )

    observation = result.observations[0]
    assert observation.quality_assessment == "rejected"
    assert "clipped" in observation.quality_reasons
    assert observation.quality_details["music_dominance"] == "not_assessed"
    assert observation.embedding is None


def test_voice_music_heuristic_rejects_only_strong_multi_feature_tonal_pattern(
    tmp_path,
):
    paths, hashes = _voice_models(tmp_path)

    result = analyze_voice(
        _sine_wave(440),
        model_paths=paths,
        expected_hashes=hashes,
        runtime=FullWindowVoiceRuntime(),
    )

    observation = result.observations[0]
    details = observation.quality_details["music_dominance_heuristic"]
    assert observation.quality_assessment == "rejected"
    assert observation.quality_reasons == ("strong_possible_music_dominance_heuristic",)
    assert details["status"] == "strong_possible_music_signal_pattern"
    assert set(details["cues"]) >= {
        "low_spectral_flatness",
        "concentrated_spectral_peaks",
        "stable_dominant_frequency",
        "low_spectral_flux",
        "stable_spectral_centroid",
    }
    assert observation.embedding is None


def test_voice_music_heuristic_is_not_assessed_for_short_tonal_evidence(tmp_path):
    paths, hashes = _voice_models(tmp_path)

    result = analyze_voice(
        _sine_wave(440, seconds=1.0),
        model_paths=paths,
        expected_hashes=hashes,
        runtime=FullWindowVoiceRuntime(),
    )

    observation = result.observations[0]
    details = observation.quality_details["music_dominance_heuristic"]
    assert details["status"] == "not_assessed"
    assert "too_short" in observation.quality_reasons
    assert "music" not in " ".join(observation.quality_reasons)


def test_voice_music_heuristic_only_downgrades_possible_noisy_tonal_pattern(
    tmp_path,
):
    paths, hashes = _voice_models(tmp_path)

    result = analyze_voice(
        _noisy_tone(),
        model_paths=paths,
        expected_hashes=hashes,
        runtime=FullWindowVoiceRuntime(),
    )

    observation = result.observations[0]
    details = observation.quality_details["music_dominance_heuristic"]
    assert observation.quality_assessment == "degraded"
    assert observation.quality_reasons == ("possible_music_dominance_heuristic",)
    assert details["status"] == "possible_music_signal_pattern"
    assert observation.quality_score <= 0.6
    assert observation.embedding == pytest.approx((0.6, 0.8))


@pytest.mark.parametrize(
    "waveform",
    [
        _speech_like_signal(),
        _broadband_noise(),
    ],
    ids=("speech-like", "broadband-noise"),
)
def test_voice_music_heuristic_does_not_flag_varied_or_broadband_signals(
    tmp_path,
    waveform,
):
    paths, hashes = _voice_models(tmp_path)

    result = analyze_voice(
        waveform,
        model_paths=paths,
        expected_hashes=hashes,
        runtime=FullWindowVoiceRuntime(),
    )

    observation = result.observations[0]
    details = observation.quality_details["music_dominance_heuristic"]
    assert details["status"] == "not_flagged_by_heuristic"
    assert "music" not in " ".join(observation.quality_reasons)
    assert observation.embedding == pytest.approx((0.6, 0.8))


def test_voice_music_heuristic_records_bounded_thresholds_and_limitations(tmp_path):
    paths, hashes = _voice_models(tmp_path)
    waveform = _speech_like_signal()

    result = analyze_voice(
        waveform,
        model_paths=paths,
        expected_hashes=hashes,
        runtime=FullWindowVoiceRuntime(),
    )

    public = result.as_dict()
    observation = public["observations"][0]
    screen = observation["quality_details"]["music_dominance_heuristic"]
    provenance = public["provenance"]["music_dominance_heuristic"]
    assert public["schema_version"] == "2"
    assert screen["method"] == "deterministic_signal_heuristic"
    assert screen["revision"] == "bounded-tonal-stationarity-v1"
    assert "not a probability" in screen["limitation"]
    assert provenance["revision"] == screen["revision"]
    assert provenance["thresholds"]["maximum_frames"] == 32
    assert provenance["thresholds"]["minimum_usable_frames"] == 8
    assert provenance["thresholds"]["minimum_window_seconds"] == 1.5
    assert provenance["thresholds"]["possible_min_stationarity_cues"] == 2
    assert provenance["thresholds"]["possible_min_spectral_stationarity_cues"] == 1
    assert provenance["thresholds"]["strong_min_stationarity_cues"] == 3
    assert screen["features"]["sampled_frame_count"] == 32
    assert "music_probability" not in repr(public)
    assert "embedding" not in observation

    repeated = analyze_voice(
        waveform,
        model_paths=paths,
        expected_hashes=hashes,
        runtime=FullWindowVoiceRuntime(),
    )
    assert (
        repeated.as_dict()["observations"][0]["quality_details"][
            "music_dominance_heuristic"
        ]
        == screen
    )


def test_voice_analysis_requires_mono_sixteen_kilohertz(tmp_path):
    paths, hashes = _voice_models(tmp_path)
    with pytest.raises(ValueError, match="mono 16 kHz"):
        analyze_voice(
            [0.1] * 100,
            model_paths=paths,
            expected_hashes=hashes,
            runtime=FakeVoiceRuntime(),
            sample_rate=8_000,
        )

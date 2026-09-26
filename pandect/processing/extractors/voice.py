"""Offline Silero VAD and CAM++ voice observations.

Only mono 16 kHz normalized waveforms are accepted. Embeddings remain opaque:
they are available to the persistence layer but excluded from representations
and JSON-compatible output.
"""

import math
import statistics
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pandect.media_runner.routing import isolated, require_child
from pandect.processing.extractors.errors import (
    InvalidExtractorOutputError,
    ModelIntegrityError,
    OptionalDependencyUnavailableError,
    ResourceLimitError,
    ToolFailedError,
)
from pandect.processing.extractors.model_files import (
    LocalModelProvenance,
    validate_local_model,
)

SILERO_VAD_SHA256 = "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"
CAMPP_SHA256 = "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"
SAMPLE_RATE = 16_000
MAX_INPUT_SECONDS = 601
MIN_VOICE_WINDOW_SECONDS = 1.5
MAX_VOICE_WINDOW_SECONDS = 10.0
MUSIC_HEURISTIC_REVISION = "bounded-tonal-stationarity-v1"
MUSIC_FRAME_SAMPLES = 512
MUSIC_FRAME_HOP_SAMPLES = 256
MUSIC_MAX_FRAMES = 32
MUSIC_MIN_FRAMES = 8
MUSIC_MIN_WINDOW_RMS = 0.01
MUSIC_LOW_FREQUENCY_HZ = 80
MUSIC_HIGH_FREQUENCY_HZ = 4_000
MUSIC_HEURISTIC_THRESHOLDS: Mapping[str, float | int] = {
    "frame_samples": MUSIC_FRAME_SAMPLES,
    "frame_hop_samples": MUSIC_FRAME_HOP_SAMPLES,
    "maximum_frames": MUSIC_MAX_FRAMES,
    "minimum_usable_frames": MUSIC_MIN_FRAMES,
    "minimum_window_seconds": MIN_VOICE_WINDOW_SECONDS,
    "minimum_window_rms": MUSIC_MIN_WINDOW_RMS,
    "low_frequency_hz": MUSIC_LOW_FREQUENCY_HZ,
    "high_frequency_hz": MUSIC_HIGH_FREQUENCY_HZ,
    "possible_max_median_spectral_flatness": 0.12,
    "possible_min_median_top3_concentration": 0.50,
    "possible_min_stationarity_cues": 2,
    "possible_min_spectral_stationarity_cues": 1,
    "strong_max_median_spectral_flatness": 0.05,
    "strong_min_median_top3_concentration": 0.70,
    "strong_min_stationarity_cues": 3,
    "stable_dominant_bin_fraction": 0.75,
    "strong_stable_dominant_bin_fraction": 0.90,
    "low_mean_spectral_flux": 0.12,
    "strong_low_mean_spectral_flux": 0.06,
    "stable_centroid_cv": 0.08,
    "strong_stable_centroid_cv": 0.03,
    "stable_frame_energy_db_std": 3.0,
    "strong_stable_frame_energy_db_std": 1.0,
}
MUSIC_HEURISTIC_LIMITATION = (
    "Deterministic tonal/stationarity signal screen only; it is not a "
    "probability, a music classifier, or an inference about audio content. "
    "Voiced speech, alarms, and other sustained tones can share these cues."
)


@dataclass(frozen=True)
class VoiceModelPaths:
    """Explicit, pre-provisioned model paths. No loader downloads weights."""

    silero_vad: Path
    campplus: Path

    @classmethod
    def from_root(cls, root: Path) -> "VoiceModelPaths":
        return cls(
            silero_vad=root / "silero_vad_v4_9e2449e1.onnx",
            campplus=(
                root / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
            ),
        )


DEFAULT_VOICE_HASHES: Mapping[str, str] = {
    "silero_vad": SILERO_VAD_SHA256,
    "campplus": CAMPP_SHA256,
}


@dataclass(frozen=True)
class VadSegment:
    start_sample: int
    end_sample: int


class VoiceInferenceRuntime(Protocol):
    name: str
    version: str

    def speech_segments(
        self, samples: Sequence[float], sample_rate: int
    ) -> Sequence[VadSegment]: ...

    def embedding(
        self, samples: Sequence[float], sample_rate: int
    ) -> Sequence[float]: ...


@dataclass(frozen=True)
class MusicDominanceHeuristic:
    """Display-safe deterministic signal screen, never a content conclusion."""

    status: str
    cues: tuple[str, ...] = ()
    features: Mapping[str, float | int | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "method": "deterministic_signal_heuristic",
            "revision": MUSIC_HEURISTIC_REVISION,
            "cues": list(self.cues),
            "features": dict(self.features),
            "limitation": MUSIC_HEURISTIC_LIMITATION,
        }


@dataclass(frozen=True)
class VoiceObservation:
    unit_key: str
    start_ms: int
    end_ms: int
    quality_score: float
    quality_assessment: str
    quality_reasons: tuple[str, ...]
    quality_details: Mapping[str, Any]
    overlap_status: str
    feature_space: str
    provenance: Mapping[str, Any]
    embedding: tuple[float, ...] | None = field(repr=False)

    @property
    def sample_duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_key": self.unit_key,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "sample_duration_ms": self.sample_duration_ms,
            "quality_score": self.quality_score,
            "quality_assessment": self.quality_assessment,
            "quality_reasons": list(self.quality_reasons),
            "quality_details": dict(self.quality_details),
            "overlap_status": self.overlap_status,
            "embedding_dimension": len(self.embedding) if self.embedding else 0,
            "feature_space": self.feature_space,
            "provenance": dict(self.provenance),
            "limitations": {
                "overlap": "Speaker overlap is not assessed by this local workflow.",
                "similarity": "Voice embedding similarities are not probabilities.",
                "music_dominance": MUSIC_HEURISTIC_LIMITATION,
            },
        }


@dataclass(frozen=True)
class VoiceAnalysis:
    speech_regions: tuple[Mapping[str, int], ...]
    non_speech_regions: tuple[Mapping[str, int], ...]
    observations: tuple[VoiceObservation, ...]
    warnings: tuple[str, ...]
    provenance: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "2",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "speech_regions": [dict(value) for value in self.speech_regions],
            "non_speech_regions": [dict(value) for value in self.non_speech_regions],
            "observations": [value.as_dict() for value in self.observations],
            "warnings": list(self.warnings),
            "provenance": dict(self.provenance),
        }


def _validate_models(
    paths: VoiceModelPaths,
    expected_hashes: Mapping[str, str],
) -> dict[str, LocalModelProvenance]:
    checks = {}
    for key, path, identifier in (
        ("silero_vad", paths.silero_vad, "k2-fsa-silero-vad-v4"),
        ("campplus", paths.campplus, "3dspeaker-campp-zh-en-16k"),
    ):
        expected = expected_hashes.get(key)
        if expected is None:
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


def _normalized_waveform(values: Sequence[float]) -> array[float]:
    if isinstance(values, array) and values.typecode == "f":
        if not values or any(not math.isfinite(value) for value in values):
            raise InvalidExtractorOutputError
        if any(abs(value) > 4.0 for value in values):
            raise InvalidExtractorOutputError
        return values
    try:
        samples = array("f", (_finite_float(value) for value in values))
    except (TypeError, ValueError, OverflowError) as error:
        raise InvalidExtractorOutputError from error
    if not samples:
        raise InvalidExtractorOutputError
    if any(abs(value) > 4.0 for value in samples):
        raise InvalidExtractorOutputError
    return samples


def _normalize_embedding(values: Sequence[float]) -> tuple[float, ...]:
    vector = tuple(_finite_float(value) for value in values)
    if not vector:
        raise InvalidExtractorOutputError
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude <= 1e-12:
        raise InvalidExtractorOutputError
    return tuple(value / magnitude for value in vector)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise InvalidExtractorOutputError
    return sum(a * b for a, b in zip(left, right, strict=True))


def _normalized_segments(
    values: Sequence[VadSegment], sample_count: int
) -> tuple[VadSegment, ...]:
    result: list[VadSegment] = []
    for value in sorted(values, key=lambda part: (part.start_sample, part.end_sample)):
        if (
            not isinstance(value.start_sample, int)
            or isinstance(value.start_sample, bool)
            or not isinstance(value.end_sample, int)
            or isinstance(value.end_sample, bool)
        ):
            raise InvalidExtractorOutputError
        start = max(0, min(value.start_sample, sample_count))
        end = max(0, min(value.end_sample, sample_count))
        if end <= start:
            continue
        if result and start <= result[-1].end_sample:
            previous = result[-1]
            result[-1] = VadSegment(
                previous.start_sample, max(previous.end_sample, end)
            )
        else:
            result.append(VadSegment(start, end))
    return tuple(result)


def _windowed_segments(values: Sequence[VadSegment]) -> tuple[VadSegment, ...]:
    maximum_samples = round(MAX_VOICE_WINDOW_SECONDS * SAMPLE_RATE)
    result = []
    for value in values:
        start = value.start_sample
        while value.end_sample - start > maximum_samples:
            result.append(VadSegment(start, start + maximum_samples))
            start += maximum_samples
        if value.end_sample > start:
            result.append(VadSegment(start, value.end_sample))
    return tuple(result)


def _noise_rms(samples: Sequence[float], speech: Sequence[VadSegment]) -> float | None:
    total = 0.0
    count = 0
    cursor = 0
    for segment in speech:
        for value in samples[cursor : segment.start_sample]:
            total += value * value
            count += 1
        cursor = segment.end_sample
    for value in samples[cursor:]:
        total += value * value
        count += 1
    return math.sqrt(total / count) if count else None


def _signal_metrics(
    samples: Sequence[float], noise_rms: float | None
) -> dict[str, float | None]:
    count = len(samples)
    rms = math.sqrt(sum(value * value for value in samples) / count)
    clipping = sum(abs(value) >= 0.999 for value in samples) / count
    midpoint = count // 2
    first = samples[:midpoint]
    second = samples[midpoint:]
    first_rms = (
        math.sqrt(sum(value * value for value in first) / len(first)) if first else 0
    )
    second_rms = (
        math.sqrt(sum(value * value for value in second) / len(second)) if second else 0
    )
    quieter = min(first_rms, second_rms)
    energy_consistency_db = (
        abs(20 * math.log10(max(first_rms, second_rms) / quieter))
        if quieter > 1e-9
        else (0.0 if max(first_rms, second_rms) <= 1e-9 else 120.0)
    )
    snr = None
    if noise_rms is not None and noise_rms > 1e-9 and rms > 0:
        snr = 20 * math.log10(rms / noise_rms)
    return {
        "rms": rms,
        "clipped_sample_proportion": clipping,
        "estimated_snr_db": snr,
        "half_energy_difference_db": energy_consistency_db,
    }


def _bounded_music_frame_starts(sample_count: int) -> tuple[int, ...]:
    if sample_count < MUSIC_FRAME_SAMPLES:
        return ()
    available = 1 + (sample_count - MUSIC_FRAME_SAMPLES) // MUSIC_FRAME_HOP_SAMPLES
    if available <= MUSIC_MAX_FRAMES:
        indexes = range(available)
    else:
        indexes = (
            position * (available - 1) // (MUSIC_MAX_FRAMES - 1)
            for position in range(MUSIC_MAX_FRAMES)
        )
    return tuple(index * MUSIC_FRAME_HOP_SAMPLES for index in indexes)


def _fft_power(values: Sequence[float]) -> tuple[float, ...]:
    """Return one fixed-size power spectrum using a deterministic radix-2 FFT."""
    if len(values) != MUSIC_FRAME_SAMPLES:
        raise ValueError("music heuristic frame length is fixed")
    mean = sum(values) / MUSIC_FRAME_SAMPLES
    data = [
        complex(
            (value - mean)
            * (0.5 - 0.5 * math.cos(2 * math.pi * index / (MUSIC_FRAME_SAMPLES - 1)))
        )
        for index, value in enumerate(values)
    ]

    target = 0
    for index in range(1, MUSIC_FRAME_SAMPLES):
        bit = MUSIC_FRAME_SAMPLES >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if index < target:
            data[index], data[target] = data[target], data[index]

    width = 2
    while width <= MUSIC_FRAME_SAMPLES:
        root = complex(
            math.cos(-2 * math.pi / width),
            math.sin(-2 * math.pi / width),
        )
        half = width // 2
        for start in range(0, MUSIC_FRAME_SAMPLES, width):
            factor = 1 + 0j
            for offset in range(half):
                even = data[start + offset]
                odd = data[start + offset + half] * factor
                data[start + offset] = even + odd
                data[start + offset + half] = even - odd
                factor *= root
        width *= 2
    return tuple(
        value.real * value.real + value.imag * value.imag
        for value in data[: MUSIC_FRAME_SAMPLES // 2 + 1]
    )


def _music_dominance_heuristic(
    samples: Sequence[float],
    sample_rate: int,
) -> MusicDominanceHeuristic:
    """Screen for sustained tonal/stationary patterns without classifying content."""
    if sample_rate != SAMPLE_RATE or not samples:
        return MusicDominanceHeuristic(status="not_assessed")
    window_rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    starts = _bounded_music_frame_starts(len(samples))
    if (
        len(samples) < round(MIN_VOICE_WINDOW_SECONDS * sample_rate)
        or window_rms < MUSIC_MIN_WINDOW_RMS
        or len(starts) < MUSIC_MIN_FRAMES
    ):
        return MusicDominanceHeuristic(
            status="not_assessed",
            features={
                "window_rms": round(window_rms, 8),
                "sampled_frame_count": len(starts),
                "usable_frame_count": 0,
            },
        )

    low_bin = max(
        1,
        math.ceil(MUSIC_LOW_FREQUENCY_HZ * MUSIC_FRAME_SAMPLES / sample_rate),
    )
    high_bin = min(
        MUSIC_FRAME_SAMPLES // 2,
        math.floor(MUSIC_HIGH_FREQUENCY_HZ * MUSIC_FRAME_SAMPLES / sample_rate),
    )
    spectra: list[tuple[float, ...]] = []
    flatness_values: list[float] = []
    concentration_values: list[float] = []
    dominant_bins: list[int] = []
    centroids: list[float] = []
    frame_energy_db: list[float] = []
    minimum_frame_rms = max(MUSIC_MIN_WINDOW_RMS / 2, window_rms * 0.10)
    for start in starts:
        frame = samples[start : start + MUSIC_FRAME_SAMPLES]
        frame_rms = math.sqrt(sum(value * value for value in frame) / len(frame))
        if frame_rms < minimum_frame_rms:
            continue
        power = _fft_power(frame)[low_bin : high_bin + 1]
        total = sum(power)
        if total <= 1e-18:
            continue
        normalized = tuple(value / total for value in power)
        spectra.append(normalized)
        arithmetic_mean = total / len(power)
        epsilon = arithmetic_mean * 1e-12
        flatness_values.append(
            math.exp(sum(math.log(value + epsilon) for value in power) / len(power))
            / (arithmetic_mean + epsilon)
        )
        concentration_values.append(sum(sorted(normalized, reverse=True)[:3]))
        dominant = max(range(len(normalized)), key=normalized.__getitem__) + low_bin
        dominant_bins.append(dominant)
        centroids.append(
            sum((index + low_bin) * value for index, value in enumerate(normalized))
        )
        frame_energy_db.append(20 * math.log10(max(frame_rms, 1e-12)))

    usable = len(spectra)
    if usable < MUSIC_MIN_FRAMES:
        return MusicDominanceHeuristic(
            status="not_assessed",
            features={
                "window_rms": round(window_rms, 8),
                "sampled_frame_count": len(starts),
                "usable_frame_count": usable,
            },
        )

    median_flatness = statistics.median(flatness_values)
    median_concentration = statistics.median(concentration_values)
    median_dominant = statistics.median(dominant_bins)
    dominant_stability = (
        sum(abs(value - median_dominant) <= 1 for value in dominant_bins) / usable
    )
    mean_centroid = statistics.fmean(centroids)
    centroid_cv = (
        statistics.pstdev(centroids) / mean_centroid if mean_centroid > 0 else 0.0
    )
    energy_db_std = statistics.pstdev(frame_energy_db)
    flux_values = [
        sum(
            abs(current - previous)
            for current, previous in zip(right, left, strict=True)
        )
        / 2
        for left, right in zip(spectra, spectra[1:], strict=False)
    ]
    mean_flux = statistics.fmean(flux_values) if flux_values else 0.0

    possible_stationarity = []
    strong_stationarity = []
    if dominant_stability >= MUSIC_HEURISTIC_THRESHOLDS["stable_dominant_bin_fraction"]:
        possible_stationarity.append("stable_dominant_frequency")
    if (
        dominant_stability
        >= MUSIC_HEURISTIC_THRESHOLDS["strong_stable_dominant_bin_fraction"]
    ):
        strong_stationarity.append("stable_dominant_frequency")
    if mean_flux <= MUSIC_HEURISTIC_THRESHOLDS["low_mean_spectral_flux"]:
        possible_stationarity.append("low_spectral_flux")
    if mean_flux <= MUSIC_HEURISTIC_THRESHOLDS["strong_low_mean_spectral_flux"]:
        strong_stationarity.append("low_spectral_flux")
    if centroid_cv <= MUSIC_HEURISTIC_THRESHOLDS["stable_centroid_cv"]:
        possible_stationarity.append("stable_spectral_centroid")
    if centroid_cv <= MUSIC_HEURISTIC_THRESHOLDS["strong_stable_centroid_cv"]:
        strong_stationarity.append("stable_spectral_centroid")
    if energy_db_std <= MUSIC_HEURISTIC_THRESHOLDS["stable_frame_energy_db_std"]:
        possible_stationarity.append("stable_frame_energy")
    if energy_db_std <= MUSIC_HEURISTIC_THRESHOLDS["strong_stable_frame_energy_db_std"]:
        strong_stationarity.append("stable_frame_energy")

    possible_tonal = (
        median_flatness
        <= MUSIC_HEURISTIC_THRESHOLDS["possible_max_median_spectral_flatness"]
        and median_concentration
        >= MUSIC_HEURISTIC_THRESHOLDS["possible_min_median_top3_concentration"]
    )
    strong_tonal = (
        median_flatness
        <= MUSIC_HEURISTIC_THRESHOLDS["strong_max_median_spectral_flatness"]
        and median_concentration
        >= MUSIC_HEURISTIC_THRESHOLDS["strong_min_median_top3_concentration"]
    )
    status = "not_flagged_by_heuristic"
    cues: tuple[str, ...] = ()
    if (
        strong_tonal
        and len(strong_stationarity)
        >= MUSIC_HEURISTIC_THRESHOLDS["strong_min_stationarity_cues"]
    ):
        status = "strong_possible_music_signal_pattern"
        cues = (
            "low_spectral_flatness",
            "concentrated_spectral_peaks",
            *strong_stationarity,
        )
    elif (
        possible_tonal
        and len(possible_stationarity)
        >= MUSIC_HEURISTIC_THRESHOLDS["possible_min_stationarity_cues"]
        and len(
            {"low_spectral_flux", "stable_spectral_centroid"}
            & set(possible_stationarity)
        )
        >= MUSIC_HEURISTIC_THRESHOLDS["possible_min_spectral_stationarity_cues"]
    ):
        status = "possible_music_signal_pattern"
        cues = (
            "low_spectral_flatness",
            "concentrated_spectral_peaks",
            *possible_stationarity,
        )

    return MusicDominanceHeuristic(
        status=status,
        cues=cues,
        features={
            "window_rms": round(window_rms, 8),
            "sampled_frame_count": len(starts),
            "usable_frame_count": usable,
            "median_spectral_flatness": round(median_flatness, 6),
            "median_top3_concentration": round(median_concentration, 6),
            "dominant_bin_stability": round(dominant_stability, 6),
            "mean_spectral_flux": round(mean_flux, 6),
            "spectral_centroid_cv": round(centroid_cv, 6),
            "frame_energy_db_std": round(energy_db_std, 6),
        },
    )


def _cheap_quality(
    duration_seconds: float,
    metrics: Mapping[str, float | None],
    music_screen: MusicDominanceHeuristic,
) -> tuple[float, str, list[str]]:
    rms = float(metrics["rms"] or 0.0)
    clipping = float(metrics["clipped_sample_proportion"] or 0.0)
    snr = metrics["estimated_snr_db"]
    energy_difference = float(metrics["half_energy_difference_db"] or 0.0)
    reasons = []
    hard_reject = False
    if duration_seconds < MIN_VOICE_WINDOW_SECONDS:
        reasons.append("too_short")
        hard_reject = True
    if clipping >= 0.01:
        reasons.append("clipped")
        hard_reject = True
    elif clipping >= 0.001:
        reasons.append("some_clipping")
    if rms < 0.01:
        reasons.append("low_signal")
        hard_reject = True
    elif rms < 0.025:
        reasons.append("weak_signal")
    if snr is not None and snr < 6:
        reasons.append("noisy")
        hard_reject = True
    elif snr is not None and snr < 12:
        reasons.append("limited_snr")
    if energy_difference > 24:
        reasons.append("internally_inconsistent_energy")
        hard_reject = True
    elif energy_difference > 12:
        reasons.append("variable_energy")
    if music_screen.status == "strong_possible_music_signal_pattern":
        reasons.append("strong_possible_music_dominance_heuristic")
        hard_reject = True
    elif music_screen.status == "possible_music_signal_pattern":
        reasons.append("possible_music_dominance_heuristic")

    duration_score = min(1.0, duration_seconds / 3.0)
    signal_score = min(1.0, rms / 0.05)
    clipping_score = max(0.0, 1.0 - clipping * 100)
    snr_score = 0.75 if snr is None else min(1.0, max(0.0, snr / 20.0))
    consistency_score = max(0.0, 1.0 - energy_difference / 30.0)
    score = round(
        (duration_score + signal_score + clipping_score + snr_score + consistency_score)
        / 5,
        6,
    )
    if music_screen.status == "strong_possible_music_signal_pattern":
        score = min(score, 0.4)
    elif music_screen.status == "possible_music_signal_pattern":
        score = min(score, 0.6)
    if hard_reject:
        return score, "rejected", reasons
    return score, ("degraded" if reasons or score < 0.65 else "usable"), reasons


def analyze_voice(
    waveform: Sequence[float],
    *,
    model_paths: VoiceModelPaths,
    expected_hashes: Mapping[str, str] = DEFAULT_VOICE_HASHES,
    runtime: VoiceInferenceRuntime | None = None,
    sample_rate: int = SAMPLE_RATE,
    source_start_ms: int = 0,
) -> VoiceAnalysis:
    """Run bounded local VAD, quality gates, and CAM++ feature extraction."""
    require_child()
    if sample_rate != SAMPLE_RATE:
        raise ValueError("voice analysis requires mono 16 kHz samples")
    if source_start_ms < 0:
        raise ValueError("source_start_ms cannot be negative")
    samples = _normalized_waveform(waveform)
    if len(samples) > MAX_INPUT_SECONDS * SAMPLE_RATE:
        raise ResourceLimitError

    model_checks = _validate_models(model_paths, expected_hashes)
    inference = runtime or SherpaOnnxVoiceRuntime(model_paths)
    segments = _normalized_segments(
        inference.speech_segments(samples, sample_rate), len(samples)
    )
    windows = _windowed_segments(segments)
    background_rms = _noise_rms(samples, segments)
    model_provenance = {
        key: value.as_dict() for key, value in sorted(model_checks.items())
    }
    provenance = {
        "processor": "pandect.local-voice-observation",
        "processor_version": "1",
        "runtime": {"name": inference.name, "version": inference.version},
        "models": model_provenance,
        "preprocessing_version": "mono-16khz-float-v1",
        "inference_threads": 1,
        "music_dominance_heuristic": {
            "method": "deterministic_signal_heuristic",
            "revision": MUSIC_HEURISTIC_REVISION,
            "thresholds": dict(MUSIC_HEURISTIC_THRESHOLDS),
            "limitation": MUSIC_HEURISTIC_LIMITATION,
        },
        "training_corpus_limitation": (
            "The complete CAM++ training-corpus composition is not disclosed."
        ),
    }
    feature_space = f"campp-zh-en-16k:{model_checks['campplus'].sha256}:mono16k-v1"
    observations = []
    warnings = []
    for window in windows:
        values = samples[window.start_sample : window.end_sample]
        duration_seconds = len(values) / SAMPLE_RATE
        music_screen = _music_dominance_heuristic(values, SAMPLE_RATE)
        metrics = _signal_metrics(values, background_rms)
        score, assessment, reasons = _cheap_quality(
            duration_seconds, metrics, music_screen
        )
        embedding = None
        consistency = None
        if assessment != "rejected":
            try:
                embedding = _normalize_embedding(
                    inference.embedding(values, SAMPLE_RATE)
                )
                if duration_seconds >= 4:
                    midpoint = len(values) // 2
                    left = _normalize_embedding(
                        inference.embedding(values[:midpoint], SAMPLE_RATE)
                    )
                    right = _normalize_embedding(
                        inference.embedding(values[midpoint:], SAMPLE_RATE)
                    )
                    consistency = _cosine(left, right)
                    if consistency < 0.25:
                        reasons.append("internally_inconsistent_embedding")
                        assessment = "rejected"
                        embedding = None
                    elif consistency < 0.5:
                        reasons.append("limited_embedding_consistency")
                        assessment = "degraded"
                        score = min(score, 0.5)
            except InvalidExtractorOutputError:
                reasons.append("invalid_embedding_output")
                assessment = "rejected"
                embedding = None
                warnings.append(
                    f"voice:{window.start_sample}-{window.end_sample}:invalid_model_output"
                )

        local_start_ms = round(window.start_sample * 1000 / SAMPLE_RATE)
        local_end_ms = round(window.end_sample * 1000 / SAMPLE_RATE)
        start_ms = source_start_ms + local_start_ms
        end_ms = source_start_ms + local_end_ms
        details: dict[str, Any] = {
            "duration_seconds": round(duration_seconds, 6),
            "rms": round(float(metrics["rms"] or 0.0), 8),
            "clipped_sample_proportion": round(
                float(metrics["clipped_sample_proportion"] or 0.0), 8
            ),
            "estimated_snr_db": (
                round(float(metrics["estimated_snr_db"]), 6)
                if metrics["estimated_snr_db"] is not None
                else None
            ),
            "half_energy_difference_db": round(
                float(metrics["half_energy_difference_db"] or 0.0), 6
            ),
            "music_dominance": music_screen.status,
            "music_dominance_heuristic": music_screen.as_dict(),
            "embedding_half_cosine": (
                round(consistency, 6) if consistency is not None else None
            ),
        }
        observations.append(
            VoiceObservation(
                unit_key=f"voice:{start_ms:010d}-{end_ms:010d}",
                start_ms=start_ms,
                end_ms=end_ms,
                quality_score=round(score, 6),
                quality_assessment=assessment,
                quality_reasons=tuple(reasons),
                quality_details=details,
                overlap_status="not_assessed",
                feature_space=feature_space,
                provenance=provenance,
                embedding=embedding,
            )
        )

    speech_regions = tuple(
        {
            "start_ms": source_start_ms
            + round(value.start_sample * 1000 / SAMPLE_RATE),
            "end_ms": source_start_ms + round(value.end_sample * 1000 / SAMPLE_RATE),
        }
        for value in segments
    )
    non_speech_regions = []
    cursor = 0
    for value in segments:
        if value.start_sample > cursor:
            non_speech_regions.append(
                {
                    "start_ms": source_start_ms + round(cursor * 1000 / SAMPLE_RATE),
                    "end_ms": source_start_ms
                    + round(value.start_sample * 1000 / SAMPLE_RATE),
                }
            )
        cursor = value.end_sample
    if cursor < len(samples):
        non_speech_regions.append(
            {
                "start_ms": source_start_ms + round(cursor * 1000 / SAMPLE_RATE),
                "end_ms": source_start_ms + round(len(samples) * 1000 / SAMPLE_RATE),
            }
        )
    return VoiceAnalysis(
        speech_regions=speech_regions,
        non_speech_regions=tuple(non_speech_regions),
        observations=tuple(observations),
        warnings=tuple(warnings),
        provenance=provenance,
    )


@isolated("analyze_voice_file", source="path")
def analyze_voice_file(path: Path, *, source_start_ms: int = 0) -> VoiceAnalysis:
    """Decode normalized audio and infer without exporting PCM to the worker."""
    import tempfile

    from pandect.media_runner.operations import read_waveform

    require_child()
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        samples, sample_rate = read_waveform(path, Path(directory))
        return analyze_voice(
            samples,
            model_paths=VoiceModelPaths.from_root(Path("/models")),
            sample_rate=sample_rate,
            source_start_ms=source_start_ms,
        )


class SherpaOnnxVoiceRuntime:
    """Pinned sherpa-onnx runtime configured for one inference thread."""

    name = "sherpa-onnx"

    def __init__(self, paths: VoiceModelPaths):
        require_child()
        try:
            import sherpa_onnx
        except ImportError as error:  # pragma: no cover - runtime image concern.
            raise OptionalDependencyUnavailableError from error
        self._sherpa = sherpa_onnx
        self.version = str(getattr(sherpa_onnx, "__version__", "unknown"))
        try:
            vad_config = sherpa_onnx.VadModelConfig()
            vad_config.silero_vad.model = str(paths.silero_vad)
            vad_config.silero_vad.threshold = 0.5
            vad_config.silero_vad.min_silence_duration = 0.5
            vad_config.silero_vad.min_speech_duration = 0.25
            vad_config.silero_vad.max_speech_duration = MAX_VOICE_WINDOW_SECONDS
            vad_config.sample_rate = SAMPLE_RATE
            vad_config.num_threads = 1
            vad_config.debug = False
            self._vad_config = vad_config

            embedding_config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(paths.campplus),
                num_threads=1,
                debug=False,
                provider="cpu",
            )
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(embedding_config)
        except (RuntimeError, ValueError) as error:
            raise ToolFailedError from error

    def speech_segments(
        self, samples: Sequence[float], sample_rate: int
    ) -> Sequence[VadSegment]:
        try:
            detector = self._sherpa.VoiceActivityDetector(
                self._vad_config, buffer_size_in_seconds=60
            )
            result = []
            block_size = 512
            for start in range(0, len(samples), block_size):
                detector.accept_waveform(samples[start : start + block_size])
                while not detector.empty():
                    segment = detector.front
                    result.append(
                        VadSegment(
                            int(segment.start),
                            int(segment.start) + len(segment.samples),
                        )
                    )
                    detector.pop()
            detector.flush()
            while not detector.empty():
                segment = detector.front
                result.append(
                    VadSegment(
                        int(segment.start),
                        int(segment.start) + len(segment.samples),
                    )
                )
                detector.pop()
            return tuple(result)
        except (RuntimeError, ValueError) as error:
            raise ToolFailedError from error

    def embedding(self, samples: Sequence[float], sample_rate: int) -> Sequence[float]:
        try:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate, samples)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                raise ToolFailedError
            return self._extractor.compute(stream)
        except (RuntimeError, ValueError) as error:
            raise ToolFailedError from error

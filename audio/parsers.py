"""Pure bounded WAV inspection and recording chunks."""

import base64
import io
import math
import struct
import sys
import wave

from core.parser_contract import MAX_BYTES, ParserFailure

MAX_AUDIO_SECONDS = 1800


def _audio(content, *, inspect_only=False):
    try:
        with wave.open(io.BytesIO(content), "rb") as recording:
            channels = recording.getnchannels()
            width = recording.getsampwidth()
            rate = recording.getframerate()
            frames = recording.getnframes()
            if (
                recording.getcomptype() != "NONE"
                or width != 2
                or channels not in {1, 2}
            ):
                return {"status": "unsupported", "reason": "unsupported_audio_encoding"}
            if not 8000 <= rate <= 48000 or frames <= 0:
                return {"status": "unsupported", "reason": "unsupported_audio_rate"}
            duration = frames / rate
            if duration > MAX_AUDIO_SECONDS:
                raise ParserFailure("audio_duration_limit")
            expected = frames * width * channels
            if expected > MAX_BYTES:
                raise ParserFailure("input_limit")
            intervals = []
            offset = 0
            square_sum = 0
            sample_count = 0
            zero_samples = 0
            while offset < frames:
                count = min(rate, frames - offset)
                data = recording.readframes(count)
                if len(data) != count * width * channels:
                    raise ParserFailure("truncated_audio")
                if inspect_only:
                    offset += count
                    continue
                samples = [value[0] for value in struct.iter_unpack("<h", data)]
                clipping = sum(abs(value) >= 32760 for value in samples) / len(samples)
                zeros = sum(value == 0 for value in samples)
                square_sum += sum(value * value for value in samples)
                sample_count += len(samples)
                zero_samples += zeros
                if zeros == len(samples) or clipping >= 0.01:
                    intervals.append(
                        {
                            "start": offset / rate,
                            "end": (offset + count) / rate,
                            "digital_zero": zeros == len(samples),
                            "full_scale_fraction": clipping,
                        }
                    )
                offset += count
            return {
                "status": "complete",
                "format": "WAV",
                "codec": "PCM signed 16-bit little-endian",
                "version": (
                    f"Python {sys.version_info.major}.{sys.version_info.minor} wave"
                ),
                "channels": channels,
                "sample_width_bytes": width,
                "sample_rate": rate,
                "frames": frames,
                "duration_seconds": duration,
                "rms_sample_amplitude": math.sqrt(square_sum / sample_count)
                if sample_count
                else None,
                "digital_zero": zero_samples == sample_count if sample_count else None,
                "quality_intervals": intervals,
            }
    except wave.Error:
        return {"status": "unsupported", "reason": "unsupported_audio_format"}


def _audio_chunk(content, options):
    with wave.open(io.BytesIO(content), "rb") as recording:
        start, count = options.get("start_frame"), options.get("frame_count")
        rate = recording.getframerate()
        if (
            recording.getsampwidth() != 2
            or recording.getnchannels() not in {1, 2}
            or not 8000 <= rate <= 48000
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (start, count)
            )
            or not 0 <= start < recording.getnframes()
            or not 0 < count <= rate * 30
            or start + count > recording.getnframes()
        ):
            raise ParserFailure("invalid_audio_chunk")
        recording.setpos(start)
        data = recording.readframes(count)
        if len(data) != count * 2 * recording.getnchannels():
            raise ParserFailure("truncated_audio")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as chunk:
            chunk.setnchannels(recording.getnchannels())
            chunk.setsampwidth(2)
            chunk.setframerate(rate)
            chunk.writeframes(data)
        return {
            "audio": base64.b64encode(buffer.getvalue()).decode("ascii"),
            "start_seconds": start / rate,
            "duration_seconds": count / rate,
            "digital_zero": not any(data),
        }


def parse(operation, content, options):
    if operation == "audio_chunk":
        return _audio_chunk(content, options)
    return _audio(content, inspect_only=operation == "audio_preview")

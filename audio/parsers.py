"""Bounded WAV/AAC M4A decoding and recording chunks, without Django."""

import base64
import io
import math
import struct
import sys
import wave

import av

from core.parser_contract import MAX_BYTES, ParserFailure

MAX_AUDIO_SECONDS = 1800


def _reject_external_audio(url, flags, options):
    raise ParserFailure("external_audio_reference")


def _m4a(content):
    """Decode only self-contained, single-track AAC into bounded 16-bit PCM."""
    try:
        with av.open(
            io.BytesIO(content),
            format="mov",
            options={
                "enable_drefs": "0",
                "use_absolute_path": "0",
                "protocol_whitelist": "",
            },
            io_open=_reject_external_audio,
        ) as container:
            if len(container.streams) != 1 or len(container.streams.audio) != 1:
                raise ParserFailure("unsupported_audio_encoding")
            stream = container.streams.audio[0]
            codec = stream.codec_context
            rate, channels = codec.sample_rate, codec.channels
            if codec.name != "aac" or channels not in {1, 2}:
                raise ParserFailure("unsupported_audio_encoding")
            if not 8000 <= rate <= 48000:
                raise ParserFailure("unsupported_audio_rate")
            if stream.duration is not None:
                duration = stream.duration * stream.time_base
                if duration > MAX_AUDIO_SECONDS:
                    raise ParserFailure("audio_duration_limit")
                if duration * rate * channels * 2 > MAX_BYTES:
                    raise ParserFailure("input_limit")
            codec.thread_count = 1
            codec.options = {"err_detect": "explode"}
            resampler = av.AudioResampler(
                format="s16", layout="mono" if channels == 1 else "stereo", rate=rate
            )
            pcm = bytearray()
            samples = 0
            for packet in container.demux(stream):
                if packet.is_corrupt:
                    raise ParserFailure("malformed_audio")
                for frame in packet.decode():
                    # Chunk anchors assume a continuous original timeline from zero.
                    if (
                        frame.sample_rate != rate
                        or len(frame.layout.channels) != channels
                        or frame.pts is None
                        or abs(frame.pts * frame.time_base * rate - samples) > 1
                    ):
                        raise ParserFailure("unsupported_audio_timeline")
                    samples += frame.samples
                    if samples / rate > MAX_AUDIO_SECONDS:
                        raise ParserFailure("audio_duration_limit")
                    if samples * channels * 2 > MAX_BYTES:
                        raise ParserFailure("input_limit")
                    for decoded in resampler.resample(frame):
                        pcm.extend(
                            bytes(decoded.planes[0])[: decoded.samples * channels * 2]
                        )
            for decoded in resampler.resample(None):
                pcm.extend(bytes(decoded.planes[0])[: decoded.samples * channels * 2])
            if not samples or len(pcm) != samples * channels * 2:
                raise ParserFailure("malformed_audio")
            if (
                stream.duration is not None
                and abs(samples - stream.duration * stream.time_base * rate) > 1
            ):
                raise ParserFailure("truncated_audio")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as recording:
                recording.setnchannels(channels)
                recording.setsampwidth(2)
                recording.setframerate(rate)
                recording.writeframes(pcm)
            version = "; ".join(
                f"{name} {'.'.join(map(str, value))}"
                for name, value in av.library_versions.items()
                if name in {"libavcodec", "libavformat", "libswresample"}
            )
            return buffer.getvalue(), f"PyAV {av.__version__}; {version}"
    except av.FFmpegError:
        raise ParserFailure("malformed_audio") from None


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
                "content_type": "audio/wav",
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
    m4a = content[4:8] == b"ftyp"
    if m4a:
        try:
            content, version = _m4a(content)
        except ParserFailure as error:
            if not str(error).startswith("unsupported_audio_"):
                raise
            return {"status": "unsupported", "reason": str(error)}
    if operation == "audio_chunk":
        return _audio_chunk(content, options)
    result = _audio(content, inspect_only=operation == "audio_preview")
    if m4a and result["status"] == "complete":
        result.update(
            format="M4A",
            codec="AAC decoded to PCM signed 16-bit little-endian",
            content_type="audio/mp4",
            version=version,
        )
    return result

"""Synthetic recordings generated in memory, never retained source material."""

import io
import math
import struct

import av


def m4a_bytes(seconds=1, *, channels=1, rate=8000, tracks=1):
    buffer = io.BytesIO()
    layout = "mono" if channels == 1 else "stereo"
    frames = int(seconds * rate)
    with av.open(buffer, "w", format="mp4") as container:
        streams = [container.add_stream("aac", rate=rate) for _ in range(tracks)]
        for stream in streams:
            stream.layout = layout
        for offset in range(0, frames, 1024):
            count = min(1024, frames - offset)
            frame = av.AudioFrame(format="fltp", layout=layout, samples=count)
            frame.sample_rate = rate
            frame.pts = offset
            samples = struct.pack(
                f"<{count}f",
                *(
                    0.25 * math.sin(2 * math.pi * 440 * (offset + index) / rate)
                    for index in range(count)
                ),
            )
            for plane in frame.planes:
                plane.update(samples)
            for stream in streams:
                for packet in stream.encode(frame):
                    container.mux(packet)
        for stream in streams:
            for packet in stream.encode(None):
                container.mux(packet)
    return buffer.getvalue()

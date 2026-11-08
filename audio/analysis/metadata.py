"""Decoded recording metadata and numerical signal observations."""

import math

from core.analysis.errors import AnalysisError
from core.analysis.sources import output
from core.analysis.sources import source_item as _item
from core.analysis.sources import unsupported_source as _unsupported
from core.parsers import read_source, run_parser

from .sources import _audio_input


def audio_metadata(sources, parameters):
    items, warnings = [], []
    supported = 0
    for source in sources:
        if not _audio_input(source):
            items.append(
                _unsupported(
                    source,
                    "Select a retained audio recording or identified "
                    "call audio addition.",
                )
            )
            continue
        content, checksum = read_source(source)
        parsed = run_parser("audio", content)
        if parsed["status"] == "unsupported":
            items.append(_unsupported(source, parsed["reason"]))
            continue
        supported += 1
        values = {
            "Parser": parsed["version"],
            "Audio-byte SHA-256": checksum,
            "Decoded format": parsed["format"],
            "Decoded codec": parsed["codec"],
            "Decoded duration (seconds)": parsed["duration_seconds"],
            "Decoded sample rate (Hz)": parsed["sample_rate"],
            "Decoded channel count": parsed["channels"],
            "RMS sample amplitude (16-bit units)": parsed["rms_sample_amplitude"],
        }
        if source.kind == "audio":
            for field, key in (
                ("sample_rate", "sample_rate"),
                ("channel_count", "channels"),
                ("duration", "duration_seconds"),
            ):
                supplied = getattr(source.item, field, None)
                if field == "duration" and supplied is not None:
                    supplied = supplied.total_seconds()
                values[f"Supplied {field}"] = (
                    supplied if supplied is not None else "Unknown"
                )
                if supplied is not None and not math.isclose(
                    float(supplied), parsed[key], abs_tol=0.001
                ):
                    warnings.append(
                        f"Supplied and decoded {field} disagree "
                        f"for {source.item.title}."
                    )
        else:
            values["Retained addition ID"] = str(source.attachment.pk)
            values["Reported call duration is not a recording duration"] = (
                source.item.duration.total_seconds()
                if source.item.duration is not None
                else "Unknown"
            )
        items.append(_item(source, "Decoded recording metadata", values=values))
        for interval in parsed["quality_intervals"]:
            label = (
                "Digital-zero interval"
                if interval["digital_zero"]
                else "Near-full-scale sample interval"
            )
            items.append(
                _item(
                    source,
                    label,
                    anchors=[
                        source.anchor(
                            start_seconds=interval["start"], end_seconds=interval["end"]
                        )
                    ],
                    values={
                        "Full-scale sample fraction": interval["full_scale_fraction"],
                        "Basis": "One-second numerical PCM window; "
                        "no speech or event label",
                    },
                )
            )
        if len(items) > 5000:
            raise AnalysisError("result_limit")
    warnings.append(
        "Numerical signal observations are not diagnoses of speech, noise, "
        "editing or dropouts. A digital-zero interval can have several causes. "
        "This bounded decoder supports uncompressed signed 16-bit mono/stereo WAV only."
    )
    return output(
        f"Decoded technical metadata from {supported} supported recordings.",
        items=items,
        metrics={"Supported recordings": supported, "Selected sources": len(sources)},
        warnings=warnings,
        outcome="complete"
        if supported == len(sources)
        else "partial"
        if supported
        else "unsupported",
    )

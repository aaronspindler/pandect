"""Bounded recording validation shared by readers and private playback."""

from core.analysis.errors import AnalysisError
from core.analysis.sources import Source
from core.parsers import read_source, run_parser


def inspect_recording(item, *, kind="audio", attachment=None):
    content, checksum = read_source(Source(str(item.pk), kind, item, attachment))
    parsed = run_parser("audio_preview", content)
    if parsed.get("status") != "complete":
        raise AnalysisError(parsed.get("reason", "unsupported_audio_format"))
    return content, checksum, parsed


def playback_message(error):
    if error.code in {"source_changed", "source_unavailable"}:
        return "The retained recording is unavailable or its checksum has changed."
    if error.code in {"input_limit", "audio_duration_limit"}:
        return "Inline playback supports recordings up to 16 MiB and 30 minutes."
    return "This recording cannot be played in the reader."

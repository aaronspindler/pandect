"""Bounded transcription with source attribution and explicit inference routing."""

import math

from core.analysis.errors import AnalysisError
from core.analysis.sources import output
from core.analysis.sources import source_item as _item
from core.analysis.sources import unsupported_source as _unsupported
from core.parser_contract import MAX_TEXT
from core.parsers import read_source, run_parser

from .sources import _audio_input


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _segments(response, duration):
    text = response.get("text")
    if not isinstance(text, str) or len(text.encode()) > MAX_TEXT:
        raise AnalysisError("gateway_invalid_response")
    segments = response.get("segments", [])
    if not isinstance(segments, list) or len(segments) > 2000:
        raise AnalysisError("gateway_invalid_response")
    previous = -1
    for segment in segments:
        if not isinstance(segment, dict):
            raise AnalysisError("gateway_invalid_response")
        start, end = segment.get("start"), segment.get("end")
        words = segment.get("text")
        if (
            not _number(start)
            or not _number(end)
            or not 0 <= start < end <= duration
            or start < previous
            or not isinstance(words, str)
            or len(words.encode()) > MAX_TEXT
        ):
            raise AnalysisError("gateway_invalid_response")
        for key in ("avg_logprob", "no_speech_prob", "compression_ratio"):
            if key in segment and not _number(segment[key]):
                raise AnalysisError("gateway_invalid_response")
        if "no_speech_prob" in segment and not 0 <= segment["no_speech_prob"] <= 1:
            raise AnalysisError("gateway_invalid_response")
        previous = start
    return text, segments


def transcription(sources, parameters, *, gateway, authorize):
    items, warnings = [], []
    unsupported = partial = supported = 0
    total_text = 0
    language = parameters.get("language", "en")
    if language != "en":
        return output(
            "Only the English transcription input profile is supported.",
            outcome="unsupported",
        )
    for source in sources:
        if not _audio_input(source):
            unsupported += 1
            items.append(
                _unsupported(
                    source,
                    "Select an identified retained recording; metadata-only calls "
                    "cannot be transcribed.",
                )
            )
            continue
        content, checksum = read_source(source)
        metadata = run_parser("audio", content)
        if metadata["status"] == "unsupported":
            unsupported += 1
            items.append(_unsupported(source, metadata["reason"]))
            continue
        supported += 1
        rate, frames = metadata["sample_rate"], metadata["frames"]
        for start_frame in range(0, frames, rate * 30):
            if len(items) >= 5000:
                raise AnalysisError("result_limit")
            frame_count = min(rate * 30, frames - start_frame)
            chunk = run_parser(
                "audio_chunk",
                content,
                options={"start_frame": start_frame, "frame_count": frame_count},
            )
            offset, duration = chunk["start_seconds"], chunk["duration_seconds"]
            interval = source.anchor(
                start_seconds=offset, end_seconds=offset + duration
            )
            if chunk["digital_zero"]:
                items.append(
                    _item(
                        source,
                        "Digital-zero recording interval",
                        anchors=[interval],
                        values={
                            "Coverage": "No inference requested for "
                            "zero-valued PCM samples",
                            "Audio-byte SHA-256": checksum,
                        },
                    )
                )
                continue
            authorize()
            response = gateway.request(
                "transcription",
                {
                    "audio": chunk["audio"],
                    "task": "transcribe",
                    "language": language,
                    "vad_filter": False,
                    "condition_on_previous_text": False,
                },
            )
            authorize()
            text, segments = _segments(response, duration)
            info = response.get("transcription_info", {})
            if not isinstance(info, dict):
                raise AnalysisError("gateway_invalid_response")
            detected = info.get("language", "unknown")
            if not isinstance(detected, str) or len(detected) > 35:
                raise AnalysisError("gateway_invalid_response")
            common = {
                "Language candidate": detected,
                "Requested language": language,
                "Audio-byte SHA-256": checksum,
                "Preparation parser": metadata["version"],
                "Chunk offset (seconds)": offset,
            }
            if not segments:
                partial += 1
                total_text += len(text.encode())
                if total_text > MAX_TEXT:
                    raise AnalysisError("extracted_text_limit")
                items.append(
                    _item(
                        source,
                        "Coarse transcript candidate"
                        if text.strip()
                        else "Unresolved recording interval",
                        text=text,
                        anchors=[interval],
                        values={
                            **common,
                            "Coverage": "Only chunk boundaries available; "
                            "no segment timestamps returned",
                        },
                    )
                )
                continue
            cursor = 0
            for segment in segments:
                if segment["start"] > cursor:
                    partial += 1
                    items.append(
                        _item(
                            source,
                            "Interval without transcribed text",
                            anchors=[
                                source.anchor(
                                    start_seconds=offset + cursor,
                                    end_seconds=offset + segment["start"],
                                )
                            ],
                            values={
                                **common,
                                "Coverage": "Unresolved; no transcription "
                                "does not establish silence or absence of speech",
                            },
                        )
                    )
                cursor = max(cursor, min(segment["end"], duration))
            if cursor < duration:
                partial += 1
                items.append(
                    _item(
                        source,
                        "Interval without transcribed text",
                        anchors=[
                            source.anchor(
                                start_seconds=offset + cursor,
                                end_seconds=offset + duration,
                            )
                        ],
                        values={
                            **common,
                            "Coverage": "Unresolved; no transcription "
                            "does not establish silence or absence of speech",
                        },
                    )
                )
            joined = " ".join(segment["text"] for segment in segments)
            if " ".join(joined.split()) != " ".join(text.split()):
                partial += 1
                warnings.append(
                    "A chunk's full text differs from its timed segments; only "
                    "source-timed segment text is retained for search."
                )
            for segment in segments:
                total_text += len(segment["text"].encode())
                if not segment["text"].strip():
                    partial += 1
                uncertain = (
                    segment.get("avg_logprob", 0) < -1
                    or segment.get("no_speech_prob", 0) > 0.6
                    or segment.get("compression_ratio", 0) > 2.4
                )
                items.append(
                    _item(
                        source,
                        "Transcript segment candidate",
                        text=segment["text"],
                        anchors=[
                            source.anchor(
                                start_seconds=offset + segment["start"],
                                end_seconds=offset + min(segment["end"], duration),
                            )
                        ],
                        values={
                            **common,
                            "Model uncertainty": "Review uncertain speech"
                            if uncertain
                            else "No low-confidence flag supplied; still unverified",
                            "Average log probability": segment.get(
                                "avg_logprob", "Not supplied"
                            ),
                            "No-speech probability": segment.get(
                                "no_speech_prob", "Not supplied"
                            ),
                        },
                    )
                )
            if total_text > MAX_TEXT:
                raise AnalysisError("extracted_text_limit")
            if len(items) > 5000:
                raise AnalysisError("result_limit")
    warnings.append(
        "Transcription is a candidate reading of English 16-bit PCM WAV. "
        "Names, accents, overlap and chunk boundaries can introduce errors. "
        "Speakers are not identified or equated with call participants. "
        "Supplied transcripts stay separate. No returned text does not prove silence."
    )
    outcome = (
        "partial"
        if partial or (supported and unsupported)
        else "unsupported"
        if unsupported
        else "complete"
        if total_text
        else "empty"
    )
    return output(
        f"Transcribed {supported} supported recordings with "
        "original-timeline interval anchors.",
        items=items,
        metrics={
            "Selected sources": len(sources),
            "Supported recordings": supported,
            "Unsupported inputs": unsupported,
            "Unresolved/coarse/discrepant chunks": partial,
            "UTF-8 text bytes": total_text,
        },
        warnings=warnings,
        outcome=outcome,
    )

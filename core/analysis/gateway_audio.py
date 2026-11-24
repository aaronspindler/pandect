"""Encode bounded WAV chunks for gateway speech APIs and normalize their replies."""

from base64 import b64decode
from uuid import uuid4

from core.analysis.errors import AnalysisError
from core.analysis.inference_payloads import encode_json


def build_audio_request(execution, payload):
    api = execution["api"]
    encoded = payload.get("audio")
    if not isinstance(encoded, str) or payload.get("language") != "en":
        raise AnalysisError("gateway_invalid_input")
    if len(encoded) > 4 * ((execution["max_request_bytes"] + 2) // 3):
        raise AnalysisError("gateway_input_limit")
    try:
        wav = b64decode(encoded, validate=True)
    except ValueError:
        raise AnalysisError("gateway_invalid_input") from None
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise AnalysisError("gateway_invalid_input")
    if api in {"nova-3", "whisper-wav"}:
        return wav, "audio/wav"
    if api == "audio-transcriptions":
        # The parser supplies the bytes. Never include an evidence filename.
        boundary = uuid4().hex
        hint = "languages[]" if execution["model"] == "gpt-transcribe" else "language"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
            f"{execution['model']}\r\n"
            f'--{boundary}\r\nContent-Disposition: form-data; name="{hint}"'
            "\r\n\r\nen\r\n"
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="chunk.wav"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode("ascii")
        return body + wav + f"\r\n--{boundary}--\r\n".encode(), (
            f"multipart/form-data; boundary={boundary}"
        )
    if api == "audio-run":
        model = execution["catalog_model"]
        language_key = (
            "language_code" if model.startswith("elevenlabs/") else "language"
        )
        inputs = {"file": f"data:audio/wav;base64,{encoded}", language_key: "en"}
        if model.startswith("elevenlabs/"):
            inputs.update(diarize=False, tag_audio_events=False)
        return encode_json({"model": model, "input": inputs}), "application/json"
    raise AnalysisError("gateway_not_configured")


def decode_audio_response(execution, result):
    """Keep supplied timing, leaving validation against chunk duration to audio."""
    try:
        if execution["api"] == "nova-3":
            # Regular HTTP uses one mixed channel unless multichannel is requested.
            (channel,) = result["results"]["channels"]
            alternative = channel["alternatives"][0]
            text, words = alternative["transcript"], alternative.get("words", [])
            language = channel.get("detected_language", "unknown")
        else:
            text, words = result["text"], result.get("words", [])
            language = (
                result.get("language_code") or result.get("language") or "unknown"
            )
            if result.get("languages"):
                language = result["languages"][0]["code"]
        if (
            not isinstance(text, str)
            or not isinstance(words, list)
            or len(words) > 2000
        ):
            raise ValueError
        segments = result.get("segments", [])
        if not isinstance(segments, list):
            raise ValueError
        word_timing = bool(words and not segments)
        if word_timing:
            for word in words:
                if word.get("type") != "spacing":
                    value = (
                        word.get("punctuated_word")
                        or word.get("word")
                        or word.get("text")
                    )
                    segments.append(
                        {"start": word["start"], "end": word["end"], "text": value}
                    )
        return {
            "text": text,
            "segments": segments,
            "word_timing": word_timing,
            "transcription_info": {"language": language},
        }
    except AttributeError, IndexError, KeyError, TypeError, ValueError:
        raise AnalysisError("gateway_invalid_response") from None

"""Bounded clients for operator-owned llama.cpp and whisper.cpp runtimes."""

import base64
import binascii
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from django.conf import settings

from .errors import AnalysisError
from .http import NoRedirect
from .inference_payloads import decode_json, encode_json, evidence_messages


class LocalClient:
    """Use the run's retained execution identity, with no external fallback."""

    def __init__(self, authorize, execution):
        self.authorize = authorize
        self.execution = execution
        self.usage = {
            "request_count": 0,
            "request_bytes": 0,
            "response_bytes": 0,
            "input_tokens": None,
            "output_tokens": None,
            "elapsed_seconds": 0,
        }
        self._validate_configuration()

    def _validate_configuration(self):
        config = self.execution
        if not isinstance(config, dict):
            raise AnalysisError("local_ai_not_configured")
        url = config.get("url", "")
        try:
            parsed = urlsplit(url)
            valid = (
                config.get("backend") == "local"
                and config.get("purpose") in {"text", "transcription"}
                and parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and not any(character.isspace() for character in url)
                and re.fullmatch(r"[a-fA-F0-9]{64}", config.get("sha256", ""))
                and isinstance(config.get("model"), str)
                and 0 < len(config["model"]) <= 255
                and isinstance(config.get("runtime"), str)
                and 0 < len(config["runtime"]) <= 128
            )
            for name in (
                "timeout",
                "max_request_bytes",
                "max_response_bytes",
                "max_output_tokens",
            ):
                valid = valid and type(config.get(name)) is int and config[name] > 0
            if config.get("purpose") == "text":
                valid = (
                    valid
                    and type(config.get("context_tokens")) is int
                    and config["context_tokens"] > config["max_output_tokens"]
                )
        except ValueError, TypeError, KeyError:
            valid = False
        if not valid:
            raise AnalysisError("local_ai_not_configured")

    def _request(self, path, payload=None, *, content_type="application/json"):
        config = self.execution
        try:
            body = payload if isinstance(payload, bytes) else encode_json(payload)
        except ValueError, TypeError, RecursionError:
            raise AnalysisError("local_ai_invalid_input") from None
        if payload is None:
            body = None
        if body is not None and len(body) > config["max_request_bytes"]:
            raise AnalysisError("local_ai_input_limit")
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if body is not None:
            # The runtime checks this identity under its request lock before
            # reading evidence or forwarding it to the native engine. A prior
            # GET alone cannot pin a runtime replaced between HTTP requests.
            headers["X-Pandect-Identity"] = base64.urlsafe_b64encode(
                encode_json(self._expected_identity())
            ).decode("ascii")
        token = settings.PANDECT_LOCAL_AI.get("token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            config["url"].rstrip("/") + path,
            data=body,
            headers=headers,
            method="GET" if body is None else "POST",
        )
        self.authorize()
        started = time.monotonic()
        self.usage["request_count"] += 1
        self.usage["request_bytes"] += len(body or b"")
        try:
            # A local endpoint is an explicit operator trust decision. Never
            # route evidence through a process's ambient HTTP proxy or redirect.
            with build_opener(ProxyHandler({}), NoRedirect()).open(
                request, timeout=config["timeout"]
            ) as response:
                raw = response.read(config["max_response_bytes"] + 1)
            self.usage["response_bytes"] += len(raw)
            if len(raw) > config["max_response_bytes"]:
                raise AnalysisError("local_ai_output_limit")
            result = decode_json(raw)
            if not isinstance(result, dict) or "error" in result:
                raise AnalysisError("local_ai_invalid_response")
            return result
        except HTTPError as error:
            code = {
                401: "local_ai_authentication",
                403: "local_ai_authentication",
                409: "local_ai_model_changed",
                413: "local_ai_input_limit",
                429: "local_ai_busy",
                503: "local_ai_busy",
                504: "local_ai_unavailable",
            }.get(error.code, "local_ai_unavailable")
            error.close()
            raise AnalysisError(code) from None
        except URLError, TimeoutError, OSError:
            raise AnalysisError("local_ai_unavailable") from None
        except ValueError, TypeError, RecursionError:
            raise AnalysisError("local_ai_invalid_response") from None
        finally:
            self.usage["elapsed_seconds"] = round(
                self.usage["elapsed_seconds"] + time.monotonic() - started, 3
            )
            self.authorize()

    def _expected_identity(self):
        purpose = self.execution["purpose"]
        expected = {
            "kind": purpose,
            "model": self.execution["model"],
            "sha256": self.execution["sha256"],
            "runtime": self.execution["runtime"],
        }
        if purpose == "text":
            expected["context_tokens"] = self.execution["context_tokens"]
        return expected

    def _identity(self, purpose):
        if self.execution["purpose"] != purpose:
            raise AnalysisError("local_ai_not_configured")
        actual = self._request("/identity")
        expected = self._expected_identity()
        if any(actual.get(key) != value for key, value in expected.items()):
            raise AnalysisError("local_ai_model_changed")

    def infer(self, prompt, schema):
        try:
            messages = evidence_messages(prompt, schema)
        except ValueError, TypeError, RecursionError:
            raise AnalysisError("local_ai_invalid_input") from None
        self._identity("text")
        template = {
            "messages": messages,
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
        }
        rendered = self._request("/apply-template", template).get("prompt")
        if not isinstance(rendered, str):
            raise AnalysisError("local_ai_invalid_response")
        tokens = self._request(
            "/tokenize",
            {"content": rendered, "add_special": True, "parse_special": True},
        ).get("tokens")
        if not isinstance(tokens, list) or any(
            type(token) is not int or not 0 <= token < 2**31 for token in tokens
        ):
            raise AnalysisError("local_ai_invalid_response")
        if (
            len(tokens) + self.execution["max_output_tokens"]
            > self.execution["context_tokens"]
        ):
            raise AnalysisError("local_ai_context_limit")
        self.usage["input_tokens"] = (self.usage["input_tokens"] or 0) + len(tokens)
        result = self._request(
            "/v1/chat/completions",
            template
            | {
                "model": self.execution["model"],
                "temperature": 0,
                "max_tokens": self.execution["max_output_tokens"],
                "stream": False,
                "cache_prompt": False,
                "response_format": {"type": "json_schema", "schema": schema},
            },
        )
        try:
            choices = result["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            if (
                choice["finish_reason"] != "stop"
                or message.get("reasoning_content")
                or message.get("tool_calls")
                or result.get("model", self.execution["model"])
                != self.execution["model"]
            ):
                raise ValueError
            value = decode_json(message["content"])
            if not isinstance(value, (dict, list)):
                raise ValueError
        except (
            KeyError,
            IndexError,
            ValueError,
            TypeError,
            AttributeError,
            RecursionError,
        ):
            raise AnalysisError("local_ai_invalid_response") from None
        usage = result.get("usage", {})
        if isinstance(usage, dict):
            count = usage.get("completion_tokens")
            if type(count) is int and 0 <= count < 2**31:
                self.usage["output_tokens"] = (self.usage["output_tokens"] or 0) + count
        return value

    def request(self, purpose, payload):
        if purpose != "transcription":
            raise AnalysisError("local_ai_invalid_input")
        if (
            not isinstance(payload, dict)
            or payload.get("language") != "en"
            or payload.get("task") != "transcribe"
            or payload.get("vad_filter") is not False
            or payload.get("condition_on_previous_text") is not False
        ):
            raise AnalysisError("local_ai_invalid_input")
        try:
            encoded = payload["audio"]
            if not isinstance(encoded, str):
                raise ValueError
            if len(encoded) > 4 * self.execution["max_request_bytes"] // 3 + 4:
                raise AnalysisError("local_ai_input_limit")
            audio = base64.b64decode(encoded, validate=True)
            if not audio.startswith(b"RIFF") or audio[8:12] != b"WAVE":
                raise ValueError
        except KeyError, ValueError, TypeError, binascii.Error:
            raise AnalysisError("local_ai_invalid_input") from None
        self._identity("transcription")
        boundary = "pandect-" + uuid4().hex
        parts = []
        fields = {
            "response_format": "verbose_json",
            "language": "en",
            "translate": "false",
            "temperature": "0",
            "temperature_inc": "0",
            "vad": "false",
            "no_context": "true",
            "no_timestamps": "false",
            "no_language_probabilities": "true",
        }
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                f"\r\n\r\n{value}\r\n".encode()
            )
        parts.extend(
            [
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                'filename="recording.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
                audio,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        result = self._request(
            "/inference",
            b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        text, segments = result.get("text"), result.get("segments", [])
        if not isinstance(text, str) or not isinstance(segments, list):
            raise AnalysisError("local_ai_invalid_response")
        if len(segments) > 2000:
            raise AnalysisError("local_ai_output_limit")
        normalized = []
        previous = -1
        for segment in segments:
            if not isinstance(segment, dict):
                raise AnalysisError("local_ai_invalid_response")
            start, end = segment.get("start"), segment.get("end")
            if (
                type(start) not in {int, float}
                or type(end) not in {int, float}
                or not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 <= start < end <= 30
                or start < previous
                or not isinstance(segment.get("text"), str)
            ):
                raise AnalysisError("local_ai_invalid_response")
            previous = start
            value = {"start": start, "end": end, "text": segment["text"]}
            for key in ("avg_logprob", "no_speech_prob", "compression_ratio"):
                if key in segment:
                    number = segment[key]
                    if type(number) not in {int, float} or not math.isfinite(number):
                        raise AnalysisError("local_ai_invalid_response")
                    if key == "no_speech_prob" and not 0 <= number <= 1:
                        raise AnalysisError("local_ai_invalid_response")
                    value[key] = number
            normalized.append(value)
        language = result.get("language", "unknown")
        if not isinstance(language, str) or len(language) > 35:
            raise AnalysisError("local_ai_invalid_response")
        return {
            "text": text,
            "segments": normalized,
            "transcription_info": {"language": language},
        }

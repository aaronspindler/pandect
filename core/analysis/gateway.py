"""Bounded external inference with per-request authorization and usage accounting."""

import re
import time
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404

from core.analysis import chat_usage
from core.analysis.errors import AnalysisError
from core.analysis.execution import gateway_execution
from core.analysis.http import NoRedirect, provider_failure_status
from core.analysis.inference_payloads import decode_json, encode_json, evidence_messages
from core.analysis.model_catalog import MODELS, catalog_execution, model_purpose
from core.models import GatewayRequest


def gateway_configured(purpose, operation):
    """Check the selected external AI configuration without contacting a provider."""
    return gateway_route_configured(purpose, gateway_execution(purpose, operation))


def gateway_route_configured(purpose, execution):
    """Validate a resolved route against the installation's credentials and limits."""
    config = settings.PANDECT_AI_GATEWAY
    try:
        if any(
            type(value) is not int or value <= 0
            for value in (
                execution["timeout"],
                execution["max_request_bytes"],
                execution["max_response_bytes"],
            )
        ):
            return False
        model, provider = execution["model"], execution["provider"]
        if execution.get("url"):
            prefix = "gpt" if provider == "openai" else "claude"
            return bool(
                re.fullmatch(rf"{prefix}-[A-Za-z0-9._-]{{1,100}}", model)
                and re.fullmatch(
                    r"[!-~]+", settings.PANDECT_DIRECT_AI.get(f"{provider}_api_key", "")
                )
            )
        account, gateway = execution["account"], execution["gateway"]
        workers = provider == "workers-ai"
        valid_gateway = bool(
            re.fullmatch(r"[a-fA-F0-9]{32}", account)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", gateway)
            and config["gateway_token"]
        )
        if not valid_gateway:
            return False
        if "catalog_model" in execution:
            catalog_id = execution["catalog_model"]
            record = MODELS.get(catalog_id)
            if not record or model_purpose(record) != purpose:
                return False
            resolved = catalog_execution(catalog_id)
            if any(execution.get(key) != value for key, value in resolved.items()):
                return False
            if resolved["billing"] == "gateway-credits" and not config.get(
                "unified_billing"
            ):
                return False
            if resolved["billing"] == "byok" and not re.fullmatch(
                r"[A-Za-z0-9_-]{1,100}", resolved["key_alias"]
            ):
                return False
            return resolved["transport"] == "provider-native" or bool(
                config.get("workers_token")
            )
        return bool(
            purpose in {"text", "transcription"}
            and (
                workers
                and len(model) <= 255
                and re.fullmatch(r"@cf/[A-Za-z0-9_./-]+", model)
                and not any(part in {".", "..", ""} for part in model.split("/"))
                and config["workers_token"]
                or provider == "openai"
                and re.fullmatch(r"gpt-[A-Za-z0-9._-]{1,100}", model)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", execution["key_alias"])
            )
        )
    except KeyError, TypeError, AttributeError:
        return False


def endpoint_and_headers(execution):
    headers = {"Content-Type": "application/json", "User-Agent": "Pandect/0.1"}
    provider = execution["provider"]
    if execution.get("url"):
        key = settings.PANDECT_DIRECT_AI[f"{provider}_api_key"]
        if provider == "anthropic":
            headers["X-Api-Key"] = key
            headers["anthropic-version"] = execution["api_version"]
        else:
            headers["Authorization"] = f"Bearer {key}"
        return execution["url"], headers

    config = settings.PANDECT_AI_GATEWAY
    headers.update(
        {
            "cf-aig-authorization": f"Bearer {config['gateway_token']}",
            "cf-aig-skip-cache": "true",
            "cf-aig-collect-log": "false",
            "cf-aig-collect-log-payload": "false",
        }
    )
    if execution.get("billing") == "byok" or execution.get("key_alias"):
        headers["cf-aig-no-wholesale"] = "true"
    if execution.get("transport") == "cloudflare-rest":
        api = execution["api"]
        path = {
            "chat-completions": "v1/chat/completions",
            "responses": "v1/responses",
            "messages": "v1/messages",
            "audio-run": "run",
        }.get(api)
        if api in {"workers-ai", "nova-3", "whisper-wav"}:
            path = f"run/{execution['catalog_model']}"
        if path is None:
            raise AnalysisError("gateway_not_configured")
        headers.update(
            {
                "Authorization": f"Bearer {config['workers_token']}",
                "cf-aig-gateway-id": execution["gateway"],
                "cf-aig-allowed-providers": provider,
                "cf-aig-request-timeout": str(execution["timeout"] * 1000),
            }
        )
        if api == "messages":
            headers["anthropic-version"] = "2023-06-01"
        return (
            f"https://api.cloudflare.com/client/v4/accounts/{execution['account']}/ai/{path}",
            headers,
        )
    if provider == "workers-ai":
        path = f"workers-ai/{execution['model']}"
        headers["Authorization"] = f"Bearer {config['workers_token']}"
    else:
        if execution.get("purpose") == "embedding":
            path = "openai/embeddings"
        else:
            path = {
                "audio-transcriptions": "openai/audio/transcriptions",
                "chat-completions": "openai/chat/completions",
                "responses": "openai/responses",
            }[execution["api"]]
        if execution.get("key_alias"):
            headers["cf-aig-byok-alias"] = execution["key_alias"]
        headers["cf-aig-request-timeout"] = str(execution["timeout"] * 1000)
    url = (
        f"https://gateway.ai.cloudflare.com/v1/"
        f"{execution['account']}/{execution['gateway']}/{path}"
    )
    return url, headers


class GatewayClient:
    def __init__(self, run, authorize, *, chat_turn=None):
        self.run = run
        self.authorize = authorize
        self.chat_turn = chat_turn

    def _execution(self, purpose):
        if self.chat_turn is not None:
            return self.chat_turn.execution
        return self.run.parameters.get("_execution") or gateway_execution(
            purpose, self.run.operation
        )

    def request(self, purpose, payload):
        execution = self._execution(purpose)
        external = (
            execution.get("location") == "external"
            if self.chat_turn is not None
            else self.run.external_processing
        )
        if not external:
            raise AnalysisError("external_processing_not_approved")
        if execution.get("purpose", purpose) != purpose or not gateway_route_configured(
            purpose, execution
        ):
            raise AnalysisError("gateway_not_configured")
        model, provider = execution["model"], execution["provider"]
        api = execution["api"]
        catalog_audio = (
            purpose == "transcription"
            and "catalog_model" in execution
            and api != "workers-ai"
        )
        content_type = "application/json"
        output_key = "max_output_tokens" if api == "responses" else "max_tokens"
        if self.chat_turn is not None and self.chat_turn.mode != "full":
            payload = {output_key: 4096} | payload
        try:
            if catalog_audio:
                from core.analysis.gateway_audio import build_audio_request

                body, content_type = build_audio_request(execution, payload)
            else:
                if api != "workers-ai":
                    payload = payload | {"model": model}
                body = encode_json(payload)
        except ValueError, TypeError, RecursionError:
            raise AnalysisError("gateway_invalid_input") from None
        if len(body) > execution["max_request_bytes"]:
            raise AnalysisError("gateway_input_limit")
        request_size = len(body)
        for attempt in range(2):
            with transaction.atomic():
                case = self.authorize(lock=True)
                reservation = chat_usage.reserve(
                    self.chat_turn,
                    "answer",
                    request_size,
                    output_limit=payload.get(output_key, 0),
                )
                if reservation:
                    body = encode_json(payload | {output_key: reservation.output_limit})
                usage = GatewayRequest.objects.create(
                    case=case,
                    run=self.run,
                    model=model,
                    request_bytes=len(body),
                    **(
                        {"chat_turn": self.chat_turn, "operation": "evidence_chat"}
                        if self.chat_turn is not None
                        else {}
                    ),
                )
            try:
                self.authorize()
                if not gateway_route_configured(purpose, execution):
                    raise AnalysisError("gateway_not_configured")
                url, headers = endpoint_and_headers(execution)
                headers["Content-Type"] = content_type
                request = Request(url, data=body, headers=headers, method="POST")
                with build_opener(NoRedirect()).open(
                    request, timeout=execution["timeout"]
                ) as response:
                    raw = response.read(execution["max_response_bytes"] + 1)
                if len(raw) > execution["max_response_bytes"]:
                    raise AnalysisError("gateway_output_limit")
                result = decode_json(raw)
                if not isinstance(result, dict) or result.get("success") is False:
                    raise AnalysisError("gateway_invalid_response")
                if (
                    api == "audio-run"
                    and result.get("state", "Completed") != "Completed"
                ):
                    raise AnalysisError("gateway_incomplete_response")
                if not execution.get("url"):
                    result = result.get("result", result)
                if not isinstance(result, dict):
                    raise AnalysisError("gateway_invalid_response")
                tokens = result.get("usage", {})
                if catalog_audio:
                    from core.analysis.gateway_audio import decode_audio_response

                    result = decode_audio_response(execution, result)
                usage.status = "complete"
                usage.response_bytes = len(raw)
                if isinstance(tokens, dict):
                    token_keys = ("input_tokens", "output_tokens")
                    if api in {"workers-ai", "chat-completions"}:
                        token_keys = ("prompt_tokens", "completion_tokens")
                    for field, key in zip(
                        ("input_tokens", "output_tokens"), token_keys, strict=True
                    ):
                        value = tokens.get(key)
                        if (
                            isinstance(value, int)
                            and not isinstance(value, bool)
                            and 0 <= value < 2**31
                        ):
                            setattr(usage, field, value)
                usage.save()
                if reservation:
                    input_tokens = usage.input_tokens
                    if api == "messages" and input_tokens is not None:
                        # Anthropic reports cached tokens outside its input total.
                        for key in (
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        ):
                            cached = tokens.get(key, 0)
                            if type(cached) is not int or not 0 <= cached < 2**31:
                                input_tokens = None
                                break
                            input_tokens += cached
                    reservation.settle(input_tokens, usage.output_tokens)
                    if (
                        usage.output_tokens is not None
                        and usage.output_tokens > reservation.output_limit
                    ):
                        raise AnalysisError("gateway_output_limit")
                self.authorize()
                return result
            except PermissionDenied, Http404:
                usage.status = "access_removed"
                usage.save(update_fields=["status"])
                raise
            except HTTPError as error:
                usage.status, usage.response_bytes = provider_failure_status(error)
                usage.save(update_fields=["status", "response_bytes"])
                if attempt == 0 and (
                    usage.status == "rate_limit"
                    or error.code in {502, 503, 504}
                    or (provider == "anthropic" and error.code == 529)
                ):
                    self.authorize()
                    time.sleep(1)
                    continue
                raise AnalysisError("gateway_" + usage.status) from None
            except OSError, HTTPException:
                usage.status = "unavailable"
                usage.save(update_fields=["status"])
                raise AnalysisError("gateway_unavailable") from None
            except ValueError, TypeError, RecursionError:
                usage.status = "invalid_response"
                usage.save(update_fields=["status"])
                raise AnalysisError("gateway_invalid_response") from None
            except AnalysisError as error:
                if usage.status == "pending":
                    usage.status = (
                        "cancelled"
                        if error.code
                        in {
                            "processing_cancelled",
                            "external_processing_not_approved",
                            "execution_changed",
                            "gateway_not_configured",
                            "source_archived",
                        }
                        else "invalid_response"
                    )
                    usage.save(update_fields=["status"])
                raise
        raise AnalysisError("gateway_unavailable")

    def infer(self, prompt, schema):
        try:
            messages = evidence_messages(prompt, schema)
        except ValueError, TypeError, RecursionError:
            raise AnalysisError("gateway_invalid_input") from None
        execution = self._execution("text")
        api = execution["api"]
        if api == "messages":
            payload = {"system": messages[0]["content"], "messages": messages[1:]}
        else:
            payload = {"input" if api == "responses" else "messages": messages}
        result = self.request("text", payload | execution.get("inference_settings", {}))
        if api == "responses":
            response = self._openai_text(result)
        elif api == "chat-completions":
            response = self._chat_text(result)
        elif api == "messages":
            response = self._anthropic_text(result)
        else:
            response = result.get("response")
        if not isinstance(response, str):
            raise AnalysisError("gateway_invalid_response")
        try:
            value = decode_json(response)
        except ValueError, RecursionError:
            raise AnalysisError("gateway_invalid_response") from None
        if not isinstance(value, (dict, list) if api == "workers-ai" else dict):
            raise AnalysisError("gateway_invalid_response")
        return value

    @staticmethod
    def _chat_text(result):
        choices = result.get("choices")
        if (
            not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(choices[0], dict)
        ):
            raise AnalysisError("gateway_invalid_response")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise AnalysisError("gateway_invalid_response")
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise AnalysisError("gateway_refused")
        if choice.get("finish_reason") != "stop":
            raise AnalysisError("gateway_incomplete_response")
        if message.get("tool_calls") or message.get("function_call"):
            raise AnalysisError("gateway_invalid_response")
        return message.get("content")

    @staticmethod
    def _anthropic_text(result):
        if result.get("type") != "message" or result.get("role") != "assistant":
            raise AnalysisError("gateway_invalid_response")
        stop_reason = result.get("stop_reason")
        if stop_reason == "refusal":
            raise AnalysisError("gateway_refused")
        if stop_reason != "end_turn":
            raise AnalysisError("gateway_incomplete_response")
        content = result.get("content")
        if not isinstance(content, list) or not content:
            raise AnalysisError("gateway_invalid_response")
        texts = []
        for part in content:
            if not isinstance(part, dict):
                raise AnalysisError("gateway_invalid_response")
            if part.get("type") in ("thinking", "redacted_thinking"):
                continue
            if part.get("type") != "text" or not isinstance(part.get("text"), str):
                raise AnalysisError("gateway_invalid_response")
            texts.append(part["text"])
        return "".join(texts)

    @staticmethod
    def _openai_text(result):
        if result.get("status") != "completed":
            raise AnalysisError("gateway_incomplete_response")
        output = result.get("output")
        if not isinstance(output, list):
            raise AnalysisError("gateway_invalid_response")
        texts = []
        for item in output:
            if not isinstance(item, dict):
                raise AnalysisError("gateway_invalid_response")
            if item.get("type") == "reasoning":
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") != "completed"
            ):
                raise AnalysisError("gateway_invalid_response")
            content = item.get("content")
            if not isinstance(content, list):
                raise AnalysisError("gateway_invalid_response")
            for part in content:
                if not isinstance(part, dict):
                    raise AnalysisError("gateway_invalid_response")
                if part.get("type") == "refusal":
                    raise AnalysisError("gateway_refused")
                if part.get("type") != "output_text" or not isinstance(
                    part.get("text"), str
                ):
                    raise AnalysisError("gateway_invalid_response")
                texts.append(part["text"])
        return "".join(texts)

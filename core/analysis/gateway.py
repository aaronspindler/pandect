"""Bounded external inference with per-request authorization and usage accounting."""

import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.analysis.execution import gateway_execution, validate_execution
from core.analysis.http import NoRedirect
from core.analysis.inference_payloads import decode_json, encode_json, evidence_messages
from core.models import GatewayRequest


def gateway_configured(purpose, operation):
    """Check the selected external AI configuration without contacting a provider."""
    config = settings.PANDECT_AI_GATEWAY
    try:
        execution = gateway_execution(purpose, operation)
        model, provider = execution["model"], execution["provider"]
        if execution.get("url"):
            prefix = "gpt" if provider == "openai" else "claude"
            return bool(
                re.fullmatch(rf"{prefix}-[A-Za-z0-9._-]{{1,100}}", model)
                and re.fullmatch(
                    r"[!-~]+", settings.PANDECT_DIRECT_AI.get(f"{provider}_api_key", "")
                )
            )
        account, gateway = config["account"], config["gateway"]
        workers = provider == "workers-ai"
        return bool(
            purpose in {"text", "transcription"}
            and re.fullmatch(r"[a-fA-F0-9]{32}", account)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", gateway)
            and config["gateway_token"]
            and (
                workers
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


def _endpoint_and_headers(execution):
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
        }
    )
    if provider == "workers-ai":
        path = f"workers-ai/{execution['model']}"
        headers["Authorization"] = f"Bearer {config['workers_token']}"
    else:
        path = "openai/responses"
        headers["cf-aig-byok-alias"] = execution["key_alias"]
        headers["cf-aig-request-timeout"] = str(execution["timeout"] * 1000)
    url = (
        f"https://gateway.ai.cloudflare.com/v1/"
        f"{execution['account']}/{execution['gateway']}/{path}"
    )
    return url, headers


class GatewayClient:
    def __init__(self, run, authorize):
        self.run = run
        self.authorize = authorize

    def request(self, purpose, payload):
        if not gateway_configured(purpose, self.run.operation):
            raise AnalysisError("gateway_not_configured")
        execution = gateway_execution(purpose, self.run.operation)
        model, provider = execution["model"], execution["provider"]
        workers = provider == "workers-ai"
        if not self.run.external_processing:
            raise AnalysisError("external_processing_not_approved")
        validate_execution(self.run)
        try:
            if not workers:
                payload = payload | {"model": model}
            body = encode_json(payload)
        except ValueError, TypeError, RecursionError:
            raise AnalysisError("gateway_invalid_input") from None
        if len(body) > execution["max_request_bytes"]:
            raise AnalysisError("gateway_input_limit")
        url, headers = _endpoint_and_headers(execution)
        for attempt in range(2):
            validate_execution(self.run)
            if gateway_execution(purpose, self.run.operation) != execution:
                raise AnalysisError("execution_changed")
            with transaction.atomic():
                case = self.authorize(lock=True)
                usage = GatewayRequest.objects.create(
                    case=case, run=self.run, model=model, request_bytes=len(body)
                )
            request = Request(url, data=body, headers=headers, method="POST")
            try:
                self.authorize()
                validate_execution(self.run)
                if gateway_execution(purpose, self.run.operation) != execution:
                    raise AnalysisError("execution_changed")
                with build_opener(NoRedirect()).open(
                    request, timeout=execution["timeout"]
                ) as response:
                    raw = response.read(execution["max_response_bytes"] + 1)
                if len(raw) > execution["max_response_bytes"]:
                    raise AnalysisError("gateway_output_limit")
                result = decode_json(raw)
                if not isinstance(result, dict) or result.get("success") is False:
                    raise AnalysisError("gateway_invalid_response")
                if not execution.get("url"):
                    result = result.get("result", result)
                if not isinstance(result, dict):
                    raise AnalysisError("gateway_invalid_response")
                usage.status = "complete"
                usage.response_bytes = len(raw)
                tokens = result.get("usage", {})
                if isinstance(tokens, dict):
                    for field, key in (
                        (
                            "input_tokens",
                            "prompt_tokens" if workers else "input_tokens",
                        ),
                        (
                            "output_tokens",
                            "completion_tokens" if workers else "output_tokens",
                        ),
                    ):
                        value = tokens.get(key)
                        if (
                            isinstance(value, int)
                            and not isinstance(value, bool)
                            and 0 <= value < 2**31
                        ):
                            setattr(usage, field, value)
                usage.save()
                self.authorize()
                return result
            except PermissionDenied, Http404:
                usage.status = "access_removed"
                usage.save(update_fields=["status"])
                raise
            except HTTPError as error:
                error.close()
                usage.status = {
                    401: "authentication",
                    403: "authentication",
                    402: "quota",
                    429: "rate_limit",
                }.get(error.code, "provider_failure")
                usage.save(update_fields=["status"])
                if attempt == 0 and (
                    error.code in {429, 502, 503, 504}
                    or (provider == "anthropic" and error.code == 529)
                ):
                    self.authorize()
                    time.sleep(1)
                    continue
                raise AnalysisError("gateway_" + usage.status) from None
            except URLError, TimeoutError, OSError:
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
                        if error.code == "processing_cancelled"
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
        execution = gateway_execution("text", self.run.operation)
        provider = execution["provider"]
        if provider == "anthropic":
            payload = {"system": messages[0]["content"], "messages": messages[1:]}
        else:
            payload = {"input" if provider == "openai" else "messages": messages}
        result = self.request("text", payload | execution.get("inference_settings", {}))
        if provider == "openai":
            response = self._openai_text(result)
        elif provider == "anthropic":
            response = self._anthropic_text(result)
        else:
            response = result.get("response")
        if not isinstance(response, str):
            raise AnalysisError("gateway_invalid_response")
        try:
            value = decode_json(response)
        except ValueError, RecursionError:
            raise AnalysisError("gateway_invalid_response") from None
        if not isinstance(value, (dict, list) if provider == "workers-ai" else dict):
            raise AnalysisError("gateway_invalid_response")
        return value

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

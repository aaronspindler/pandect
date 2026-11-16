"""Bounded Workers AI and OpenAI inference through Cloudflare AI Gateway only."""

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


class GatewayClient:
    def __init__(self, run, authorize):
        self.run = run
        self.authorize = authorize

    def request(self, purpose, payload):
        config = settings.PANDECT_AI_GATEWAY
        account, gateway = config["account"], config["gateway"]
        execution = gateway_execution(purpose, self.run.operation)
        model, provider = execution["model"], execution["provider"]
        workers = provider == "workers-ai"
        if not (
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
        ):
            raise AnalysisError("gateway_not_configured")
        if not self.run.external_processing:
            raise AnalysisError("external_processing_not_approved")
        validate_execution(self.run)
        try:
            if not workers:
                payload = payload | {"model": model}
            body = encode_json(payload)
        except ValueError, TypeError, RecursionError:
            raise AnalysisError("gateway_invalid_input") from None
        if len(body) > config["max_request_bytes"]:
            raise AnalysisError("gateway_input_limit")
        path = f"workers-ai/{model}" if workers else "openai/responses"
        url = f"https://gateway.ai.cloudflare.com/v1/{account}/{gateway}/{path}"
        for attempt in range(2):
            validate_execution(self.run)
            if gateway_execution(purpose, self.run.operation) != execution:
                raise AnalysisError("execution_changed")
            with transaction.atomic():
                case = self.authorize(lock=True)
                usage = GatewayRequest.objects.create(
                    case=case, run=self.run, model=model, request_bytes=len(body)
                )
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Pandect/0.1",
                "cf-aig-authorization": f"Bearer {config['gateway_token']}",
                "cf-aig-skip-cache": "true",
                "cf-aig-collect-log": "false",
            }
            if workers:
                headers["Authorization"] = f"Bearer {config['workers_token']}"
            else:
                headers["cf-aig-byok-alias"] = execution["key_alias"]
                headers["cf-aig-request-timeout"] = str(execution["timeout"] * 1000)
            request = Request(url, data=body, headers=headers, method="POST")
            try:
                self.authorize()
                validate_execution(self.run)
                if gateway_execution(purpose, self.run.operation) != execution:
                    raise AnalysisError("execution_changed")
                with build_opener(NoRedirect()).open(
                    request, timeout=execution["timeout"]
                ) as response:
                    raw = response.read(config["max_response_bytes"] + 1)
                if len(raw) > config["max_response_bytes"]:
                    raise AnalysisError("gateway_output_limit")
                result = decode_json(raw)
                if not isinstance(result, dict) or result.get("success") is False:
                    raise AnalysisError("gateway_invalid_response")
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
                if attempt == 0 and error.code in {429, 502, 503, 504}:
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
        openai = execution["provider"] == "openai"
        result = self.request(
            "text",
            {
                "input" if openai else "messages": messages,
                **execution.get("inference_settings", {}),
            },
        )
        response = self._openai_text(result) if openai else result.get("response")
        if not isinstance(response, str):
            raise AnalysisError("gateway_invalid_response")
        try:
            value = decode_json(response)
        except ValueError, RecursionError:
            raise AnalysisError("gateway_invalid_response") from None
        if not isinstance(value, dict if openai else (dict, list)):
            raise AnalysisError("gateway_invalid_response")
        return value

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

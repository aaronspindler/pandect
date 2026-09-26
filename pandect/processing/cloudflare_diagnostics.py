"""Evidence-free, explicit Cloudflare capability diagnostics."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from pandect.processing.remote.cloudflare import (
    CloudflareAIError,
    CloudflareConfig,
)

META_VISION_MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"


class CloudflareDiagnosticError(Exception):
    """Sanitized diagnostic failure; remote response bodies are never retained."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("Cloudflare capability diagnostic failed.")


@dataclass(frozen=True)
class CloudflareDiagnosticReport:
    credentials_valid: bool
    models_available: tuple[str, ...]
    gateway_reachable: bool
    meta_license_action: str


def _json_response(response: httpx.Response, *, code: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise CloudflareDiagnosticError(code) from error
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise CloudflareDiagnosticError(code)
    result = payload.get("result")
    if isinstance(result, dict) and "state" in result:
        if str(result["state"]).casefold() != "completed":
            raise CloudflareDiagnosticError(code)
    return payload


def _contains_model(value: Any, model: str) -> bool:
    if isinstance(value, str):
        return value.casefold() == model.casefold()
    if isinstance(value, dict):
        return any(_contains_model(child, model) for child in value.values())
    if isinstance(value, list):
        return any(_contains_model(child, model) for child in value)
    return False


def diagnose_cloudflare(
    config: CloudflareConfig,
    *,
    accept_meta_license: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> CloudflareDiagnosticReport:
    """Validate credentials, models, and Gateway with fixed non-evidence inputs.

    The only state-changing operation is Meta license acceptance, and it is
    impossible unless ``accept_meta_license`` is explicitly true.
    """
    try:
        config.validate_credentials()
    except CloudflareAIError as error:
        raise CloudflareDiagnosticError("configuration_invalid") from error
    if config.timeout_seconds <= 0:
        raise CloudflareDiagnosticError("configuration_invalid")

    authorization = {"Authorization": f"Bearer {config.api_token}"}
    gateway_headers = {
        **authorization,
        "cf-aig-gateway-id": config.gateway_id,
        "cf-aig-collect-log-payload": "false",
        "cf-aig-skip-cache": "true",
        "cf-aig-request-timeout": "120000",
        "cf-aig-max-attempts": "1",
        "cf-aig-metadata": '{"operation":"diagnostic"}',
    }
    base = "https://api.cloudflare.com/client/v4/accounts/" + quote(
        config.account_id, safe=""
    )
    timeout = httpx.Timeout(min(config.timeout_seconds, 300), connect=10)
    configured_models = tuple(
        dict.fromkeys(
            (config.text_model, config.vision_model, config.transcription_model)
        )
    )

    try:
        with httpx.Client(
            transport=transport, timeout=timeout, trust_env=False
        ) as client:
            available = []
            for model in configured_models:
                response = client.get(
                    f"{base}/ai/models/search",
                    headers=authorization,
                    params={"search": model, "per_page": "10"},
                )
                payload = _json_response(
                    response,
                    code="credentials_or_model_search_failed",
                )
                if not _contains_model(payload.get("result"), model):
                    raise CloudflareDiagnosticError("configured_model_unavailable")
                available.append(model)

            meta_models = tuple(
                model for model in configured_models if model == META_VISION_MODEL
            )
            license_action = "not_applicable"
            if meta_models:
                license_action = "unchanged"
                if accept_meta_license:
                    response = client.post(
                        f"{base}/ai/run/{quote(META_VISION_MODEL, safe='@/')}",
                        headers={
                            **gateway_headers,
                            "Content-Type": "application/json",
                        },
                        json={"prompt": "agree", "max_tokens": 1},
                    )
                    _json_response(response, code="meta_license_acceptance_failed")
                    license_action = "accepted_or_reaffirmed"

            response = client.post(
                f"{base}/ai/run",
                headers={
                    **gateway_headers,
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.text_model,
                    "input": {
                        "prompt": "Reply with the exact word ready.",
                        "max_tokens": 3,
                        "temperature": 0,
                    },
                },
            )
            _json_response(response, code="gateway_or_model_execution_failed")
    except CloudflareDiagnosticError:
        raise
    except httpx.HTTPError as error:
        raise CloudflareDiagnosticError("network_failure") from error

    return CloudflareDiagnosticReport(
        credentials_valid=True,
        models_available=tuple(available),
        gateway_reachable=True,
        meta_license_action=license_action,
    )

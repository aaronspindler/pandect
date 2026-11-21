"""Snapshot concrete execution routes without equating HTTP with cloud use."""

import json
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.crypto import salted_hmac

from core.analysis.errors import AnalysisError

GEOGRAPHIC = {
    "geocode_locations": ("GEOCODER", "geocode"),
    "geographic_context": ("MAP_CONTEXT", "map_context"),
}

OPENAI_TEXT_MODELS = {
    "text_topics": "gpt-6-luna",
    "content_events": "gpt-6-luna",
    "session_summaries": "gpt-6.1-terra",
    "conversation_link": "gpt-6.1-terra",
    "question_synthesis": "gpt-6.2-sol",
}

DIRECT_AI_ENDPOINTS = {
    "openai-direct": ("openai", "responses", "https://api.openai.com/v1/responses"),
    "anthropic-direct": (
        "anthropic",
        "messages",
        "https://api.anthropic.com/v1/messages",
    ),
}


def gateway_execution(purpose, operation):
    """Retain the selected provider and method, never its credentials."""
    config = settings.PANDECT_AI_GATEWAY
    provider = (
        config.get("text_provider", "workers-ai") if purpose == "text" else "workers-ai"
    )
    limits = {
        key: config[key]
        for key in ("timeout", "max_request_bytes", "max_response_bytes")
    }
    if provider in DIRECT_AI_ENDPOINTS:
        provider, api, url = DIRECT_AI_ENDPOINTS[provider]
        execution = limits | {
            "provider": provider,
            "api": api,
            "url": url,
            "adapter_version": "direct-1",
            "model": settings.PANDECT_DIRECT_AI.get(f"{provider}_model", ""),
            "timeout": 300,
        }
        if provider == "openai":
            execution["inference_settings"] = {
                "store": False,
                "max_output_tokens": 32768,
                "text": {"format": {"type": "json_object"}},
            }
        else:
            execution["api_version"] = "2023-06-01"
            execution["inference_settings"] = {"max_tokens": 8192}
        return execution
    execution = {
        "provider": provider,
        "api": "responses" if provider == "openai" else "workers-ai",
        "adapter_version": "gateway-3",
        "account": config["account"],
        "gateway": config["gateway"],
        "model": config["models"].get(purpose, ""),
        **limits,
    }
    if provider == "openai":
        if execution["model"] == "auto":
            execution["model"] = OPENAI_TEXT_MODELS.get(operation, "")
        execution["adapter_version"] = "gateway-4"
        execution["timeout"] = 300
        execution["key_alias"] = config.get("openai_key_alias", "default")
        execution["inference_settings"] = {
            "store": False,
            "reasoning": {
                "effort": "high"
                if operation
                in {"question_synthesis", "content_events", "conversation_link"}
                else "medium"
            },
            "max_output_tokens": 32768,
            "text": {"format": {"type": "json_object"}},
        }
    elif purpose == "text":
        if provider == "workers-ai" and execution["model"] == "auto":
            execution["model"] = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        execution["inference_settings"] = {"max_tokens": 4096, "temperature": 0}
    return execution


def external_ai_destination(execution):
    provider = {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "workers-ai": "Workers AI",
    }.get(execution.get("provider", "workers-ai"))
    if not provider:
        return "an external AI service the operator must select"
    model = execution.get("model") or "model not configured"
    if execution.get("url"):
        return f"{provider} · {model}"
    destination = f"{provider} via Cloudflare AI Gateway · {model}"
    if execution.get("account") and execution.get("gateway"):
        destination += f" · {execution['account']} / {execution['gateway']}"
    return destination


def execution_destination(execution):
    """Display the reviewed service without credentials or URL paths."""
    if execution["backend"] == "gateway":
        return external_ai_destination(execution)
    try:
        endpoint = urlsplit(execution.get("url", ""))
    except ValueError:
        return ""
    if endpoint.scheme in {"http", "https"} and endpoint.hostname:
        return f"{endpoint.scheme}://{endpoint.netloc.rsplit('@', 1)[-1]}"
    return ""


def inference_configured(execution, operation):
    """Check the current settings without contacting the processing service."""
    if execution["backend"] == "gateway":
        from core.analysis.gateway import gateway_configured

        return gateway_configured(execution["purpose"], operation)
    from core.analysis.local import validate_local_configuration

    try:
        validate_local_configuration(execution)
    except AnalysisError:
        return False
    return True


def execution_confirmation(salt, scope, execution):
    """Bind a workflow's review identity to its current processing route(s)."""
    value = json.dumps([*scope, execution], sort_keys=True)
    return salted_hmac(salt, value, algorithm="sha256").hexdigest()


def requires_inference(operation, parameters):
    if operation == "transcription":
        return "transcription"
    if (
        operation in {"content_events", "session_summaries", "question_synthesis"}
        or (operation == "text_topics" and parameters.get("include_summary", True))
        or (
            operation == "conversation_link" and parameters.get("semantic_links", False)
        )
    ):
        return "text"
    return None


def processing_external(operation, parameters, backend="local"):
    if requires_inference(operation, parameters):
        if backend not in {"local", "gateway"}:
            raise ValidationError("Choose local or external AI processing.")
        return backend == "gateway"
    if operation in GEOGRAPHIC:
        prefix, _ = GEOGRAPHIC[operation]
        return not getattr(settings, f"PANDECT_{prefix}_LOCAL", False)
    return False


def snapshot_execution(operation, parameters):
    """Only network-backed operations need a route, never a credential snapshot."""
    purpose = requires_inference(operation, parameters)
    if purpose:
        backend = parameters.get("backend", "local")
        external = processing_external(operation, parameters, backend)
        common = {
            "backend": backend,
            "purpose": purpose,
            "location": "external" if external else "local",
            "adapter_version": "2",
        }
        if backend == "gateway":
            return common | gateway_execution(purpose, operation)
        config = getattr(settings, "PANDECT_LOCAL_AI", {})
        if purpose == "text":
            common["driver"] = "ollama"
            common["adapter_version"] = "ollama-1"
        # Fixed decoding choices are part of the retained method too. Bump the
        # adapter version when changing request construction or interpretation.
        common["inference_settings"] = (
            {
                "temperature": 0,
                "think": False,
                "truncate": False,
                "shift": False,
                "keep_alive": 0,
                "context_bound": "rendered_utf8_bytes+8",
            }
            if purpose == "text"
            else {
                "language": "en",
                "task": "transcribe",
                "temperature": 0,
                "temperature_inc": 0,
                "vad_filter": False,
                "condition_on_previous_text": False,
            }
        )
        return (
            common
            | {
                key: config.get(purpose, {}).get(key, "")
                for key in ("url", "model", "sha256", "runtime")
            }
            | {
                key: config.get(key)
                for key in (
                    "timeout",
                    "max_request_bytes",
                    "max_response_bytes",
                    "max_output_tokens",
                )
            }
            | (
                {"context_tokens": config.get("text", {}).get("context_tokens")}
                if purpose == "text"
                else {}
            )
        )
    if operation in GEOGRAPHIC:
        prefix, purpose = GEOGRAPHIC[operation]
        external = processing_external(operation, parameters)
        snapshot = {
            "backend": "external" if external else "local",
            "location": "external" if external else "local",
            "purpose": purpose,
        }
        for key in ("URL", "NAME", "VERSION", "ATTRIBUTION", "USER_AGENT", "TIMEOUT"):
            snapshot[key.lower()] = getattr(settings, f"PANDECT_{prefix}_{key}", "")
        if prefix == "MAP_CONTEXT":
            snapshot["history_start"] = settings.PANDECT_MAP_CONTEXT_HISTORY_START
        return snapshot
    return None


def retry_execution_confirmation(actor_id, run_id, submission_id, execution):
    """Bind approval to the investigator, new submission, and reviewed route."""
    return execution_confirmation(
        "analysis.retry.execution",
        [str(actor_id), str(run_id), str(submission_id)],
        execution,
    )


def validate_execution(run):
    """Old runs keep their old route; new work cannot drift after queueing."""
    execution = run.parameters.get("_execution")
    purpose = requires_inference(run.operation, run.parameters)
    if (
        execution is None
        and run.external_processing
        and purpose
        and gateway_execution(purpose, run.operation)["provider"] != "workers-ai"
    ):
        raise AnalysisError("execution_changed")
    if execution is not None and execution != snapshot_execution(
        run.operation, run.parameters
    ):
        raise AnalysisError("execution_changed")
    return execution

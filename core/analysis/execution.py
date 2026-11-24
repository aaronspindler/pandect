"""Snapshot concrete execution routes without equating HTTP with cloud use."""

import json
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.crypto import salted_hmac

from core.analysis.errors import AnalysisError
from core.analysis.model_catalog import (
    MODELS,
    PROVIDER_NAMES,
    catalog_execution,
    model_group,
    model_purpose,
)
from core.analysis.operations import OPERATIONS

GEOGRAPHIC = {
    name: spec.geographic for name, spec in OPERATIONS.items() if spec.geographic
}

OPENAI_TEXT_MODELS = {
    "text_topics": "gpt-6-luna",
    "content_events": "gpt-6-luna",
    "session_summaries": "gpt-6.1-terra",
    "conversation_link": "gpt-6.1-terra",
    "question_synthesis": "gpt-6.2-sol",
    "evidence_chat": "gpt-6.2-sol",
}

DIRECT_AI_ENDPOINTS = {
    "openai-direct": ("openai", "responses", "https://api.openai.com/v1/responses"),
    "anthropic-direct": (
        "anthropic",
        "messages",
        "https://api.anthropic.com/v1/messages",
    ),
}

INFERENCE_OPERATIONS = (
    *(
        name
        for name, spec in OPERATIONS.items()
        if spec.inference and not spec.private_inference
    ),
    "evidence_chat",
)


def gateway_execution(purpose, operation, *, case=None):
    """Retain the selected provider and method, never its credentials."""
    execution = _default_gateway_execution(purpose, operation)
    model = case.external_analysis_models.get(operation) if case is not None else None
    if not model:
        return _installation_execution(purpose, execution, execution["model"])
    if not isinstance(model, str):
        return execution | {"model": ""}
    model = model_form_value(operation, model, case=case)
    # An unrecognized pin is intentionally unavailable after provider changes.
    if model in _installation_models(purpose, execution):
        return _installation_execution(purpose, execution, model)
    record = MODELS.get(model)
    if record and model_purpose(record) == purpose:
        return catalog_execution(model)
    return execution | {"model": ""}


def _installation_execution(purpose, default, model):
    if (
        purpose == "transcription"
        and isinstance(model, str)
        and model.startswith("@cf/")
        and (record := MODELS.get(model))
    ):
        if model_purpose(record) != purpose:
            return default | {"model": ""}
        # Keep the established JSON adapter for compatible Workers AI models.
        if record["api"] != "workers-ai":
            return catalog_execution(model)
    return default | {"model": model}


def _default_gateway_execution(purpose, operation):
    config = settings.PANDECT_AI_GATEWAY
    provider = (
        config.get("text_provider", "workers-ai") if purpose == "text" else "workers-ai"
    )
    limits = {
        key: config.get(key)
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
        "account": config.get("account", ""),
        "gateway": config.get("gateway", ""),
        "model": config.get("models", {}).get(purpose, ""),
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
                in {
                    "question_synthesis",
                    "evidence_chat",
                    "content_events",
                    "conversation_link",
                }
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


def _installation_models(purpose, execution):
    config = settings.PANDECT_AI_GATEWAY
    models = [execution["model"]]
    if (
        purpose == "text"
        and execution["provider"] == "openai"
        and not execution.get("url")
        and config["models"].get("text") == "auto"
    ):
        models.extend(OPENAI_TEXT_MODELS.values())
    models.extend(config.get("model_choices", {}).get(purpose, ()))
    return tuple(dict.fromkeys(models))


def _installation_provider(operation):
    if operation == "transcription":
        return "workers-ai"
    return settings.PANDECT_AI_GATEWAY.get("text_provider", "workers-ai")


def pin_model_choice(operation, value):
    """Persist the configured route for the original provider's custom IDs."""
    purpose = "transcription" if operation == "transcription" else "text"
    default = _default_gateway_execution(purpose, operation)
    if value and value in _installation_models(purpose, default):
        return f"{_installation_provider(operation)}:{value}"
    return value


def model_form_value(operation, saved, *, case):
    """Unpack matching installation pins and recover exact legacy recipients."""
    current = _installation_provider(operation)
    if ":" in saved:
        provider, model = saved.split(":", 1)
        purpose = "transcription" if operation == "transcription" else "text"
        if provider == current and model in _installation_models(
            purpose, _default_gateway_execution(purpose, operation)
        ):
            return model
        return saved
    if "/" not in saved or saved.startswith("@cf/"):
        old = case.external_analysis_approval.get("executions", {}).get(operation, {})
        if old.get("model") == saved:
            old_provider = old["provider"] + ("-direct" if old.get("url") else "")
            if old_provider != current:
                return f"{old_provider}:{saved}"
    return saved


def external_model_options(operation):
    """Display all compatible models, including those awaiting operator setup."""
    from core.analysis.gateway import gateway_route_configured

    if operation not in INFERENCE_OPERATIONS:
        return ()
    purpose = "transcription" if operation == "transcription" else "text"
    default = _default_gateway_execution(purpose, operation)
    rows = []
    covered = set()
    for model in _installation_models(purpose, default):
        route = _installation_execution(purpose, default, model)
        if not model or not gateway_route_configured(purpose, route):
            continue
        provider = route["provider"]
        canonical = model if model.startswith("@cf/") else f"{provider}/{model}"
        record = MODELS.get(canonical)
        group = model_group(canonical, provider)
        if default.get("url"):
            group += " (direct)"
        else:
            covered.add(canonical)
        rows.append(
            {
                "value": model,
                "label": record["label"] if record else model,
                "group": group,
                "available": True,
            }
        )
    for model, record in MODELS.items():
        if model in covered or model_purpose(record) != purpose:
            continue
        route = catalog_execution(model)
        rows.append(
            {
                "value": model,
                "label": record["label"],
                "group": model_group(model, route["provider"]),
                "available": gateway_route_configured(purpose, route),
            }
        )
    return tuple(rows)


def external_ai_destination(execution):
    provider_id = execution.get("provider", "workers-ai")
    provider = (
        "Workers AI"
        if provider_id == "workers-ai"
        else PROVIDER_NAMES.get(provider_id, provider_id)
    )
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


def inference_configured(execution):
    """Check the current settings without contacting the processing service."""
    if execution["backend"] == "gateway":
        from core.analysis.gateway import gateway_route_configured

        return gateway_route_configured(execution["purpose"], execution)
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
    if operation == "evidence_chat":
        return "text"
    spec = OPERATIONS.get(operation)
    if spec is None:
        return None
    if spec.optional_inference:
        flag, default = spec.optional_inference
        if not parameters.get(flag, default):
            return None
    return spec.inference


def processing_external(operation, parameters, backend="local"):
    if requires_inference(operation, parameters):
        if backend not in {"local", "gateway"}:
            raise ValidationError("Choose local or external AI processing.")
        if operation not in INFERENCE_OPERATIONS and backend != "local":
            raise ValidationError("Claim comparison uses private processing.")
        return backend == "gateway"
    if operation in GEOGRAPHIC:
        prefix, _ = GEOGRAPHIC[operation]
        return not getattr(settings, f"PANDECT_{prefix}_LOCAL", False)
    return False


def snapshot_execution(operation, parameters, *, case=None):
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
            return common | gateway_execution(purpose, operation, case=case)
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


def configured_executions(operation, parameters, *, case=None):
    """Resolve configured routes without contacting an inference or map service."""
    if requires_inference(operation, parameters):
        routes = {}
        backends = (
            ("local", "gateway") if operation in INFERENCE_OPERATIONS else ("local",)
        )
        for backend in backends:
            execution = snapshot_execution(
                operation, parameters | {"backend": backend}, case=case
            )
            if inference_configured(execution):
                routes[backend] = execution
        return routes
    if operation not in GEOGRAPHIC:
        return {"local": None}

    from geo.analysis.external.geocoding import Geocoder
    from geo.analysis.external.geographic_context import ContextClient

    client = Geocoder if operation == "geocode_locations" else ContextClient
    try:
        client()
    except AnalysisError, ValueError:
        return {}
    execution = snapshot_execution(operation, parameters)
    return {execution["backend"]: execution}


def configured_external_executions(case=None):
    """Return the full external inventory for one case owner's review."""
    executions = {}
    for operation in (*INFERENCE_OPERATIONS, *GEOGRAPHIC):
        specification = OPERATIONS.get(operation)
        parameters = {}
        if specification and specification.optional_inference:
            parameters[specification.optional_inference[0]] = True
        routes = configured_executions(operation, parameters, case=case)
        external = routes.get("gateway") or routes.get("external")
        if external is not None:
            executions[operation] = external
    return executions


def available_executions(case, operation, parameters):
    """Include external work only within a current case-owner approval."""
    return {
        backend: execution
        for backend, execution in configured_executions(
            operation, parameters, case=case
        ).items()
        if execution is None
        or execution["location"] == "local"
        or case_execution_grant(case, operation, execution)
    }


def selected_ai_executions(case, operation, parameters):
    """Use the case's selected AI backend only if it can run this method."""
    available = available_executions(case, operation, parameters)
    backend = case.ai_backend
    return {backend: available[backend]} if backend in available else {}


def transfer_scope(operation, execution):
    """The recipient and material covered by an owner review."""
    if operation in GEOGRAPHIC:
        return {"operation": operation, "execution": execution}
    if operation not in INFERENCE_OPERATIONS or execution.get("backend") != "gateway":
        return None
    provider = execution["provider"]
    default_billing = (
        "direct"
        if execution.get("url")
        else ("workers-ai" if provider == "workers-ai" else "byok")
    )
    return {
        "disclosure": 1,
        "purpose": execution["purpose"],
        "provider": provider,
        "account": execution.get("account", ""),
        "gateway": execution.get("gateway", ""),
        "url": execution.get("url", ""),
        "key_alias": execution.get("key_alias", ""),
        "billing": execution.get("billing", default_billing),
        "retention": execution.get("retention", "provider-policy"),
    }


def case_execution_grants(case, operation, execution):
    """Return every valid receipt for this execution, preferred receipt first."""
    approval = case.external_analysis_approval
    if (
        not execution
        or execution["location"] != "external"
        or approval.get("approved_by") != str(case.owner_id)
    ):
        return {}
    scope = transfer_scope(operation, execution)
    grants = {
        grant["revision"]: grant
        for grant in approval.get("grants", [])
        if grant["scope"] == scope
    }
    # Queued work may retain an earlier exact-model receipt for this operation.
    if approval.get("executions", {}).get(operation) == execution:
        grants[approval["revision"]] = approval
    return grants


def case_execution_grant(case, operation, execution, *, revision=None):
    grants = case_execution_grants(case, operation, execution)
    return (
        next(iter(grants.values()), None) if revision is None else grants.get(revision)
    )


def case_grant_revision(case, operation, execution):
    grant = case_execution_grant(case, operation, execution)
    return grant["revision"] if grant else ""


def batch_grant_revision(scope, operation):
    # Old batches pinned one exact-approval revision for all their operations.
    if "external_grants" in scope:
        return scope["external_grants"].get(operation)
    return scope.get("external_grant_revision")


def validate_case_execution_grant(case, operation, execution, revision):
    grant = case_execution_grant(case, operation, execution, revision=revision)
    if not revision or not grant:
        raise AnalysisError("external_processing_not_approved")
    return grant


def validate_execution(run, case):
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
        run.operation, run.parameters, case=case
    ):
        raise AnalysisError("execution_changed")
    return execution

"""Pin approved Cloudflare inference routes and geographic service destinations."""

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
    reasoning_efforts,
    set_reasoning_effort,
)
from core.analysis.operations import OPERATIONS

GEOGRAPHIC = {
    name: spec.geographic for name, spec in OPERATIONS.items() if spec.geographic
}

TEXT_MODEL_CHOICE_PREFIXES = ("openai/", "anthropic/")

OPENAI_TEXT_MODELS = {
    "text_topics": "gpt-6-luna",
    "content_events": "gpt-6-luna",
    "session_summaries": "gpt-6.1-terra",
    "conversation_link": "gpt-6.1-terra",
    "question_synthesis": "gpt-6.2-sol",
    "evidence_chat": "gpt-6.2-sol",
    "claim_comparison": "gpt-6.2-sol",
    "narrative_relationships": "gpt-6-luna",
}

# OpenAI Responses models with image input and text output.
OPENAI_VISION_MODELS = (
    "gpt-5.4-mini",
    "gpt-6-luna",
    "gpt-6.1-terra",
    "gpt-6.2-sol",
)

INFERENCE_OPERATIONS = (
    *(name for name, spec in OPERATIONS.items() if spec.inference),
    "evidence_chat",
)


def inference_purpose(operation):
    spec = OPERATIONS.get(operation)
    return spec.inference if spec and spec.inference else "text"


def gateway_execution(purpose, operation, *, case=None):
    """Retain the selected provider and method, never its credentials."""
    execution = _selected_gateway_execution(purpose, operation, case)
    if case is not None and purpose == "text":
        set_reasoning_effort(execution, case.external_analysis_efforts.get(operation))
    return execution


def _selected_gateway_execution(purpose, operation, case):
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
    execution = {
        "provider": provider,
        "api": "responses" if provider == "openai" else "workers-ai",
        "adapter_version": "gateway-3",
        "account": config.get("account", ""),
        "gateway": config.get("gateway", ""),
        "model": config.get("models", {}).get(purpose, ""),
        **limits,
    }
    if purpose == "vision":
        return execution | {
            "provider": "openai",
            "api": "responses",
            "model": config.get("models", {}).get("vision", "gpt-4.1-mini"),
            "key_alias": config.get("openai_key_alias", "default"),
            "inference_settings": {"store": False, "max_output_tokens": 4096},
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
    if purpose == "vision":
        models.extend(OPENAI_VISION_MODELS)
    models.extend(config.get("model_choices", {}).get(purpose, ()))
    return tuple(dict.fromkeys(models))


def _installation_provider(operation):
    if inference_purpose(operation) == "vision":
        return "openai"
    if operation == "transcription":
        return "workers-ai"
    return settings.PANDECT_AI_GATEWAY.get("text_provider", "workers-ai")


def pin_model_choice(operation, value):
    """Persist the configured route for the original provider's custom IDs."""
    purpose = inference_purpose(operation)
    default = _default_gateway_execution(purpose, operation)
    if value and value in _installation_models(purpose, default):
        return f"{_installation_provider(operation)}:{value}"
    return value


def model_form_value(operation, saved, *, case):
    """Unpack matching installation pins and recover exact legacy recipients."""
    current = _installation_provider(operation)
    if ":" in saved:
        provider, model = saved.split(":", 1)
        purpose = inference_purpose(operation)
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
    """List case-selectable models, including those awaiting operator setup."""
    from core.analysis.gateway import gateway_route_configured

    if operation not in INFERENCE_OPERATIONS:
        return ()
    purpose = inference_purpose(operation)
    default = _default_gateway_execution(purpose, operation)
    rows = []
    covered = set()
    for model in _installation_models(purpose, default):
        route = _installation_execution(purpose, default, model)
        if not model or not gateway_route_configured(purpose, route):
            continue
        provider = route["provider"]
        canonical = model if model.startswith("@cf/") else f"{provider}/{model}"
        if purpose == "text" and not canonical.startswith(TEXT_MODEL_CHOICE_PREFIXES):
            continue
        record = MODELS.get(canonical)
        group = model_group(canonical, provider)
        covered.add(canonical)
        rows.append(
            {
                "value": model,
                "label": record["label"] if record else model,
                "group": group,
                "available": True,
                "efforts": reasoning_efforts(route),
                "default_effort": route.get("inference_settings", {})
                .get("reasoning", {})
                .get("effort", ""),
            }
        )
    for model, record in MODELS.items():
        if model in covered or model_purpose(record) != purpose:
            continue
        if purpose == "text" and not model.startswith(TEXT_MODEL_CHOICE_PREFIXES):
            continue
        route = catalog_execution(model)
        rows.append(
            {
                "value": model,
                "label": record["label"],
                "group": model_group(model, route["provider"]),
                "available": gateway_route_configured(purpose, route),
                "efforts": reasoning_efforts(route),
                "default_effort": "",
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
    from core.analysis.gateway import gateway_route_configured

    return execution["backend"] == "gateway" and gateway_route_configured(
        execution["purpose"], execution
    )


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


def processing_external(operation, parameters, backend="gateway"):
    if requires_inference(operation, parameters):
        if backend != "gateway":
            raise ValidationError(
                "AI analysis requires an approved Cloudflare AI Gateway destination."
            )
        return True
    if operation in GEOGRAPHIC:
        prefix, _ = GEOGRAPHIC[operation]
        return not getattr(settings, f"PANDECT_{prefix}_LOCAL", False)
    return False


def snapshot_execution(operation, parameters, *, case=None):
    """Only network-backed operations need a route, never a credential snapshot."""
    purpose = requires_inference(operation, parameters)
    if purpose:
        processing_external(operation, parameters, parameters.get("backend", "gateway"))
        return (
            {
                "backend": "gateway",
                "purpose": purpose,
                "location": "external",
            }
            | gateway_execution(purpose, operation, case=case)
            | ({"case_ai_revision": str(case.external_ai_revision)} if case else {})
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
        execution = snapshot_execution(
            operation, parameters | {"backend": "gateway"}, case=case
        )
        return {"gateway": execution} if inference_configured(execution) else {}
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
    """Resolve this method against the enabled case’s approved model routes."""
    return (
        available_executions(case, operation, parameters)
        if case.external_ai_enabled
        else {}
    )


def transfer_scope(operation, execution):
    """The recipient and material covered by an owner review."""
    if operation in GEOGRAPHIC:
        return {"operation": operation, "execution": execution}
    if operation not in INFERENCE_OPERATIONS or execution.get("backend") != "gateway":
        return None
    provider = execution["provider"]
    if execution.get("url"):
        return None
    default_billing = "workers-ai" if provider == "workers-ai" else "byok"
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
        or (operation not in GEOGRAPHIC and not case.external_ai_enabled)
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
    if purpose and (
        not run.external_processing
        or not execution
        or execution.get("backend") != "gateway"
        or execution.get("url")
    ):
        raise AnalysisError("execution_changed")
    if execution is not None and execution != snapshot_execution(
        run.operation, run.parameters, case=case
    ):
        raise AnalysisError("execution_changed")
    return execution

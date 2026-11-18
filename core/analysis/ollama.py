"""Native, digest-pinned Ollama text inference for Qwen3.5 byte-level models."""

from core.analysis.errors import AnalysisError
from core.analysis.inference_payloads import decode_json, evidence_messages


def _version(client):
    version = client._request("/api/version").get("version")
    if f"ollama@{version}" != client.execution["runtime"]:
        raise AnalysisError("local_ai_model_changed")


def pinned_model(client, *, capability, families):
    config = client.execution
    _version(client)
    models = client._request("/api/tags").get("models")
    if not isinstance(models, list):
        raise AnalysisError("local_ai_invalid_response")
    matches = [
        item
        for item in models
        if isinstance(item, dict) and item.get("name") == config["model"]
    ]
    if (
        len(matches) != 1
        or matches[0].get("digest") != config["sha256"]
        or matches[0].get("remote_host")
        or matches[0].get("remote_model")
    ):
        raise AnalysisError("local_ai_model_changed")
    # The immutable digest prevents a tag replacement between preflight and
    # dispatch. :local makes Ollama reject cloud-backed manifests server-side.
    model = f"sha256:{config['sha256']}:local"
    info = client._request("/api/show", {"model": model})
    details = info.get("details")
    capabilities = info.get("capabilities")
    if (
        info.get("remote_host")
        or info.get("remote_model")
        or not isinstance(details, dict)
        or details.get("family") not in families
        or not isinstance(capabilities, list)
        or capability not in capabilities
    ):
        raise AnalysisError("local_ai_not_configured")
    return model


def infer(client, prompt, schema):
    config = client.execution
    try:
        messages = evidence_messages(prompt, schema)
    except ValueError, TypeError, RecursionError:
        raise AnalysisError("local_ai_invalid_input") from None
    model = pinned_model(
        client, capability="completion", families={"qwen35", "qwen3_5"}
    )
    payload = {
        "model": model,
        "messages": messages,
        "format": schema,
        "stream": False,
        "think": False,
        "truncate": False,
        "shift": False,
        "keep_alive": 0,
        "options": {
            "temperature": 0,
            "num_ctx": config["context_tokens"],
            "num_predict": config["max_output_tokens"],
        },
    }
    rendered = client._request("/api/chat", payload | {"_debug_render_only": True}).get(
        "_debug_info"
    )
    if not isinstance(rendered, dict) or not isinstance(
        rendered.get("rendered_template"), str
    ):
        raise AnalysisError("local_ai_invalid_response")
    # Qwen3.5 uses byte-level BPE: UTF-8 bytes bound the token count from
    # above. Count the full server-rendered template and reserve special-token
    # overhead plus output. This deliberately rejects some selections that
    # would fit; Ollama exposes no public exact token-count endpoint.
    try:
        prompt_bound = len(rendered["rendered_template"].encode()) + 8
    except UnicodeError:
        raise AnalysisError("local_ai_invalid_response") from None
    if prompt_bound + config["max_output_tokens"] > config["context_tokens"]:
        raise AnalysisError("local_ai_context_limit")
    _version(client)
    result = client._request("/api/chat", payload)
    _version(client)
    try:
        message = result["message"]
        if (
            result.get("model") != model
            or result.get("done") is not True
            or result.get("done_reason") != "stop"
            or result.get("remote_host")
            or result.get("remote_model")
            or message.get("role") != "assistant"
            or message.get("thinking")
            or message.get("tool_calls")
        ):
            raise ValueError
        value = decode_json(message["content"])
        if not isinstance(value, (dict, list)):
            raise ValueError
        for field, key in (
            ("prompt_eval_count", "input_tokens"),
            ("eval_count", "output_tokens"),
        ):
            count = result[field]
            if type(count) is not int or not 0 <= count < 2**31:
                raise ValueError
            client.usage[key] = (client.usage[key] or 0) + count
        if (
            result["prompt_eval_count"] + config["max_output_tokens"]
            > config["context_tokens"]
            or result["eval_count"] > config["max_output_tokens"]
        ):
            raise ValueError
    except KeyError, TypeError, ValueError, AttributeError, RecursionError:
        raise AnalysisError("local_ai_invalid_response") from None
    return value

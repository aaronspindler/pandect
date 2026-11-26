"""Release-bundled models and their exact Cloudflare serving routes."""

import json
import re
from pathlib import Path

from django.conf import settings

MODELS = json.loads(Path(__file__).with_name("cloudflare_models.json").read_text())[
    "models"
]

PROVIDER_NAMES = {
    "alibaba": "Alibaba",
    "anthropic": "Anthropic",
    "deepgram": "Deepgram",
    "deepseek": "DeepSeek",
    "deepseek-ai": "DeepSeek",
    "elevenlabs": "ElevenLabs",
    "fireworks": "Fireworks AI",
    "google": "Google",
    "google-vertex-ai": "Google Vertex AI",
    "ibm-granite": "IBM",
    "meta": "Meta",
    "minimax": "MiniMax",
    "mistralai": "Mistral AI",
    "moonshotai": "Moonshot AI",
    "moonshot": "Moonshot AI",
    "nvidia": "NVIDIA",
    "openai": "OpenAI",
    "qwen": "Qwen",
    "particle": "Particle AI",
    "reflection-ai": "Reflection AI",
    "thinkingmachines": "Thinking Machines",
    "workers-ai": "Cloudflare Workers AI",
    "xai": "xAI",
    "xiaomi": "Xiaomi",
    "zai-org": "Z.ai",
}
AUDIO_APIS = {
    "audio-transcriptions",
    "audio-run",
    "nova-3",
    "whisper-wav",
    "workers-ai",
}

# OpenAI Responses models expose different ranges. Keep this independent of the
# generated Cloudflare catalog so direct and installation models use it too.
OPENAI_REASONING_EFFORTS = {
    "gpt-5": ("minimal", "low", "medium", "high"),
    "gpt-5-mini": ("minimal", "low", "medium", "high"),
    "gpt-5-nano": ("minimal", "low", "medium", "high"),
    "gpt-5.1": ("none", "low", "medium", "high"),
    "gpt-5.4": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.4-mini": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.4-nano": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.4-pro": ("medium", "high", "xhigh"),
    "gpt-5.5": ("none", "low", "medium", "high", "xhigh"),
    "gpt-5.5-pro": ("medium", "high", "xhigh"),
    "gpt-5.6": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.6-sol": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.6-terra": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6-astra": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6-sol": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6.1-terra": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6.2-astra": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6.2-sol": ("low", "medium", "high", "xhigh", "max"),
    "o3": ("low", "medium", "high"),
    "o3-mini": ("low", "medium", "high"),
    "o4-mini": ("low", "medium", "high"),
}


# Claude's direct API uses hyphens where the Cloudflare catalog uses dots.
CLAUDE_EFFORTS = ("low", "medium", "high")
CLAUDE_MAX_EFFORTS = (*CLAUDE_EFFORTS, "max")
CLAUDE_FULL_EFFORTS = (*CLAUDE_EFFORTS, "xhigh", "max")
ANTHROPIC_REASONING_EFFORTS = {
    "claude-opus-4-5": CLAUDE_EFFORTS,
    "claude-opus-4-6": CLAUDE_MAX_EFFORTS,
    "claude-sonnet-4-6": CLAUDE_MAX_EFFORTS,
    "claude-mythos-preview": CLAUDE_MAX_EFFORTS,
    "claude-opus-4-7": CLAUDE_FULL_EFFORTS,
    "claude-opus-4-8": CLAUDE_FULL_EFFORTS,
    "claude-fable-5": CLAUDE_FULL_EFFORTS,
    "claude-fable-5-1": CLAUDE_FULL_EFFORTS,
    "claude-fable-5-2": CLAUDE_FULL_EFFORTS,
    "claude-mythos-5": CLAUDE_FULL_EFFORTS,
    "claude-mythos-5-1": CLAUDE_FULL_EFFORTS,
    "claude-mythos-5-2": CLAUDE_FULL_EFFORTS,
    "claude-opus-5": CLAUDE_FULL_EFFORTS,
    "claude-opus-5-5": CLAUDE_FULL_EFFORTS,
    "claude-sonnet-5": CLAUDE_FULL_EFFORTS,
    "claude-sonnet-5-5": CLAUDE_FULL_EFFORTS,
    "claude-haiku-5-5": CLAUDE_FULL_EFFORTS,
}
REASONING_SETTINGS = {
    ("openai", "responses"): "reasoning",
    ("anthropic", "messages"): "output_config",
}


def reasoning_efforts(execution):
    provider, api = execution["provider"], execution["api"]
    if (provider, api) not in REASONING_SETTINGS:
        return ()
    model = execution["model"].removeprefix(f"{provider}/")
    if provider == "anthropic":
        model = re.sub(r"-\d{8}$", "", model).replace(".", "-")
        return ANTHROPIC_REASONING_EFFORTS.get(model, ())
    model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    return OPENAI_REASONING_EFFORTS.get(model, ())


def reasoning_setting(execution):
    return REASONING_SETTINGS.get((execution["provider"], execution["api"]))


def set_reasoning_effort(execution, effort):
    if effort and effort in reasoning_efforts(execution):
        key = reasoning_setting(execution)
        execution["inference_settings"][key] = execution["inference_settings"].get(
            key, {}
        ) | {"effort": effort}


def model_group(model, provider):
    author_id = model.removeprefix("@cf/").split("/")[0]
    author = PROVIDER_NAMES.get(author_id, author_id)
    recipient = PROVIDER_NAMES.get(provider, provider)
    if model.startswith("google/gemini-"):
        return f"Gemini via {recipient}"
    if recipient == author or recipient.startswith(f"{author} "):
        return recipient
    return f"{author} via {recipient}"


def model_purpose(model):
    return "transcription" if model["api"] in AUDIO_APIS else "text"


def catalog_execution(model_id):
    """Resolve the actual recipient from the release catalog and operator settings."""
    model = MODELS[model_id]
    config = settings.PANDECT_AI_GATEWAY
    provider = model["provider"]
    api = model["api"]
    stored = provider in config.get("stored_key_providers", ()) or (
        provider == "openai" and config.get("text_provider") == "openai"
    )
    if model_id.startswith("@cf/"):
        billing = "workers-ai"
    elif stored:
        billing = "byok"
    else:
        billing = "gateway-credits"

    # The native OpenAI path supports named key aliases. The account-scoped
    # REST API supports the default stored key and pins the serving provider.
    native = provider == "openai" and (stored or api == "audio-transcriptions")
    if native and api == "audio-run":
        api = "audio-transcriptions"
    execution = {
        "provider": provider,
        "catalog_model": model_id,
        "model": model_id.removeprefix("openai/") if native else model_id,
        "api": api,
        "transport": "provider-native" if native else "cloudflare-rest",
        "adapter_version": "catalog-1",
        "billing": billing,
        "account": config.get("account", ""),
        "gateway": config.get("gateway", ""),
        "timeout": 300,
        "max_request_bytes": config.get("max_request_bytes"),
        "max_response_bytes": config.get("max_response_bytes"),
    }
    if billing == "byok":
        execution["key_alias"] = (
            config.get("openai_key_alias", "default") if native else "default"
        )
    if model_purpose(model) == "text":
        limit = min(8192, model.get("max_tokens") or 8192)
        if api == "responses":
            execution["inference_settings"] = {
                "store": False,
                "max_output_tokens": limit,
            }
        else:
            execution["inference_settings"] = {"max_tokens": limit}
    return execution

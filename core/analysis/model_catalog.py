"""Release-bundled models and their exact Cloudflare serving routes."""

import json
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


def model_group(model, provider):
    author_id = model.removeprefix("@cf/").split("/")[0]
    author = PROVIDER_NAMES.get(author_id, author_id)
    recipient = PROVIDER_NAMES.get(provider, provider)
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

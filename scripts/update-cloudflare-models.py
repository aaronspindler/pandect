"""Build Pandect's release catalog from a pinned cloudflare/cloudflare-docs checkout."""

import argparse
import json
import subprocess
from pathlib import Path

RELEASE_CATALOG = (
    Path(__file__).resolve().parents[1] / "core/analysis/cloudflare_models.json"
)

# These model families support general-purpose analysis. Product-specific voice
# agents, safety classifiers and decision APIs do not consume Pandect's prompts.
TEXT_AUTHORS = {
    "alibaba",
    "anthropic",
    "deepseek",
    "deepseek-ai",
    "fireworks",
    "google",
    "meta",
    "minimax",
    "moonshotai",
    "openai",
    "particle",
    "reflection-ai",
    "thinkingmachines",
    "xai",
    "xiaomi",
}
WORKERS_TEXT_AUTHORS = {
    "deepseek-ai",
    "google",
    "ibm-granite",
    "meta",
    "mistralai",
    "moonshotai",
    "nvidia",
    "openai",
    "qwen",
    "zai-org",
}
AUDIO_RUN = {"elevenlabs/scribe-v3", "openai/gpt-4o-transcribe", "xai/grok-stt"}


def catalog_row(record):
    model = record["model_id"]
    author = model.split("/")[0]
    formats = record.get("request_formats") or []
    providers = [p["id"] for p in record.get("provider_details") or []]
    if not providers:
        providers = [p["provider"] for p in record.get("model_providers") or []]
    if not providers:
        return None
    provider = author if author in providers else providers[0]
    if model in AUDIO_RUN:
        api = "audio-run"
    elif author in TEXT_AUTHORS and record["task"] == "Text Generation" and formats:
        if author == "openai" and "responses" in formats:
            api = "responses"
        elif "chat-completions" in formats:
            api = "chat-completions"
        elif "anthropic-messages" in formats:
            api = "messages"
        elif "responses" in formats:
            api = "responses"
        else:
            return None
    else:
        return None
    return (model, record["name"], provider, api, record.get("max_output_tokens") or "")


def workers_row(record):
    model = record["name"]
    if record.get("deprecated") or any(
        prop.get("property_id") == "planned_deprecation_date"
        for prop in record.get("properties", [])
    ):
        return None
    author, name = model.split("/")[1:]
    if model == "@cf/openai/whisper-large-v3-turbo":
        api = "workers-ai"
    elif model == "@cf/openai/whisper":
        api = "whisper-wav"
    elif model == "@cf/deepgram/nova-3":
        api = "nova-3"
    elif (
        record["task"]["name"] == "Text Generation"
        and author in WORKERS_TEXT_AUTHORS
        and record.get("schema")
        and "guard" not in name
        and "lora" not in name
    ):
        api = "chat-completions"
    else:
        return None
    return (model, name, "workers-ai", api, "")


def write_catalog(source, revision):
    rows = []
    for folder, extract in (
        ("catalog-models", catalog_row),
        ("workers-ai-models", workers_row),
    ):
        paths = sorted((source / folder).glob("*.json"))
        if not paths:
            raise ValueError(f"Missing Cloudflare records in {folder}")
        for path in paths:
            row = extract(json.loads(path.read_text()))
            if row:
                rows.append(row)
    # OpenAI's current file transcription endpoint is exposed through native
    # Gateway routing ahead of inclusion in Cloudflare's unified model catalog.
    rows.append(
        (
            "openai/gpt-transcribe",
            "GPT Transcribe",
            "openai",
            "audio-transcriptions",
            "",
        )
    )
    models = {
        model: {
            "label": label,
            "provider": provider,
            "api": api,
            **({"max_tokens": limit} if limit else {}),
        }
        for model, label, provider, api, limit in sorted(rows)
    }
    RELEASE_CATALOG.write_text(
        json.dumps({"revision": revision, "models": models}, indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    revision = subprocess.check_output(
        ["git", "-C", args.checkout, "rev-parse", "HEAD"], text=True
    ).strip()
    content = args.checkout / "src/content"
    if subprocess.check_output(
        [
            "git",
            "-C",
            args.checkout,
            "status",
            "--porcelain",
            "--",
            "src/content/catalog-models",
            "src/content/workers-ai-models",
        ]
    ):
        parser.error("Use a checkout with unmodified model metadata.")
    write_catalog(content, revision)

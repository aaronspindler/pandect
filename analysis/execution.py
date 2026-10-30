"""Snapshot concrete execution routes without equating HTTP with cloud use."""

from django.conf import settings
from django.core.exceptions import ValidationError

from .errors import AnalysisError

GEOGRAPHIC = {
    "geocode_locations": ("GEOCODER", "geocode"),
    "geographic_context": ("MAP_CONTEXT", "map_context"),
}


def requires_inference(operation, parameters):
    if operation == "transcription":
        return "transcription"
    if (
        operation in {"content_events", "session_summaries"}
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
            config = settings.PANDECT_AI_GATEWAY
            return common | {
                "account": config["account"],
                "gateway": config["gateway"],
                "model": config["models"].get(purpose, ""),
                **{
                    key: config[key]
                    for key in (
                        "timeout",
                        "daily_requests",
                        "max_request_bytes",
                        "max_response_bytes",
                    )
                },
            }
        config = getattr(settings, "PANDECT_LOCAL_AI", {})
        # Fixed decoding choices are part of the retained method too. Bump the
        # adapter version when changing request construction or interpretation.
        common["inference_settings"] = (
            {"temperature": 0, "enable_thinking": False, "cache_prompt": False}
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


def validate_execution(run):
    """Old runs keep their old route; new work cannot drift after queueing."""
    execution = run.parameters.get("_execution")
    if execution is not None and execution != snapshot_execution(
        run.operation, run.parameters
    ):
        raise AnalysisError("execution_changed")
    return execution

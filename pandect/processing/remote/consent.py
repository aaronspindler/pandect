"""Build and hash item-specific remote transfer manifests."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConsentManifest:
    value: dict
    sha256: str


def build_adapter_privacy_contract(
    *,
    client_timeout_seconds: int,
    operation_contracts: dict[str, dict[str, Any]],
    gateway_request_timeout_seconds: int,
    gateway_retry_delay_seconds: float,
    response_limit_bytes: int,
) -> dict[str, Any]:
    """Describe the exact outbound adapter and privacy policy being consented.

    The contract deliberately excludes the API token. It includes every setting
    that can change the recipient path, request representation, retry/cost
    exposure, content logging, caching, or bounded transport behaviour. Callers
    rebuild it immediately before execution, so a setting or adapter-policy
    change invalidates an earlier consent instead of silently changing a send.
    """
    return {
        "version": "cloudflare-direct-http-v1",
        "transport": {
            "scheme": "https",
            "client_total_timeout_seconds": int(client_timeout_seconds),
            "connect_timeout_ceiling_seconds": 10,
            "gateway_request_timeout_ceiling_seconds": int(
                gateway_request_timeout_seconds
            ),
            "per_attempt_timeout_policy": (
                "minimum_of_ceiling_and_remaining_deadline_after_retry_budget"
            ),
            "response_limit_bytes": int(response_limit_bytes),
        },
        "gateway": {
            "payload_logging": False,
            "usage_cost_status_metadata_logging": True,
            "cache": False,
            "opaque_metadata_fields": ["operation", "run_id", "stage_id"],
            "retry_backoff": "exponential",
            "retry_delay_milliseconds": int(gateway_retry_delay_seconds * 1000),
        },
        "operations": {
            operation: operation_contracts[operation]
            for operation in sorted(operation_contracts)
        },
    }


def build_manifest(
    *,
    item_id: str,
    item_type: str,
    source_sha256: str,
    source_size: int,
    payloads: list[dict],
    models: list[str],
    request_count: int,
    notice_version: str,
    adapter_privacy_contract: dict[str, Any],
    prompt_versions: dict[str, str] | None = None,
    prompt_hashes: dict[str, str] | None = None,
    recipients: list[str] | None = None,
    destination: dict[str, str] | None = None,
) -> ConsentManifest:
    model_recipients = ["Cloudflare AI Gateway", *sorted(set(models))]
    value = {
        "version": "1",
        "notice_version": notice_version,
        "item_id": item_id,
        "item_type": item_type,
        "source_sha256": source_sha256,
        "source_size": source_size,
        "payloads": payloads,
        "recipients": recipients or model_recipients,
        "destination": destination or {},
        "models": models,
        "prompt_versions": prompt_versions or {},
        "prompt_hashes": prompt_hashes or {},
        "planned_request_count": request_count,
        "adapter_privacy_contract": adapter_privacy_contract,
        "gateway_payload_logging": False,
        "gateway_metadata_logging": True,
        "gateway_cache": False,
    }
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return ConsentManifest(value, hashlib.sha256(encoded).hexdigest())

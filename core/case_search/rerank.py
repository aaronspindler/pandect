"""Bounded ranking of verified search excerpts by an optional case-approved model."""

import hashlib
import math
import re
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from django.conf import settings
from django.utils import timezone

from core.analysis.errors import AnalysisError
from core.analysis.gateway import endpoint_and_headers
from core.analysis.http import NoRedirect, provider_failure_status
from core.analysis.inference_payloads import decode_json, encode_json
from core.models import ChatTurn, GatewayRequest

MAX_CANDIDATES = 24
MAX_PASSAGE_CHARS = 900
LOCAL_MODEL = "BAAI/bge-reranker-base"
HOSTED_MODEL = "@cf/baai/bge-reranker-base"
TIMEOUT = 8


def destination():
    provider = settings.PANDECT_SEARCH_RERANK_PROVIDER
    if provider == "workers-ai":
        gateway = settings.PANDECT_AI_GATEWAY
        account, name = gateway.get("account", ""), gateway.get("gateway", "")
        if (
            re.fullmatch(r"[a-fA-F0-9]{32}", account)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            and gateway.get("workers_token")
            and gateway.get("gateway_token")
        ):
            return {
                "provider": provider,
                "model": HOSTED_MODEL,
                "account": account,
                "gateway": name,
                "payload_version": 1,
            }
    if provider == "local":
        config = settings.PANDECT_LOCAL_RERANK
        url = config["url"].rstrip("/")
        try:
            parts = urlsplit(url)
            valid = (
                parts.scheme in ("http", "https")
                and parts.hostname
                and parts.username is None
                and parts.password is None
                and not parts.path
                and not parts.fragment
                and not parts.query
                and not any(c.isspace() for c in url)
                and parts.port != 0
            )
        except ValueError:
            valid = False
        if (
            valid
            and re.fullmatch(r"[a-f0-9]{40}", config["revision"])
            and re.fullmatch(r"\d+\.\d+\.\d+", config["version"])
        ):
            return {
                "provider": provider,
                "model": LOCAL_MODEL,
                "model_revision": config["revision"],
                "runtime_version": config["version"],
                "profile": hashlib.sha256(url.encode()).hexdigest(),
            }
    return {}


def rerank_scope(case):
    """Return the public execution identity to bind to the submitted search."""
    route = destination()
    if route.get("provider") != "workers-ai":
        return route
    grant = case.search_rerank_approval
    if (
        grant.get("approved_by") == str(case.owner_id)
        and grant.get("destination") == route
        and grant.get("revision")
    ):
        return route | {"revision": grant["revision"]}
    return {}


class _Unavailable(Exception):
    """Optional model failure. Source or investigator revocation propagates."""


def _read(request, *, local):
    handlers = (ProxyHandler({}), NoRedirect()) if local else (NoRedirect(),)
    try:
        with build_opener(*handlers).open(request, timeout=TIMEOUT) as response:
            raw = response.read(16 * 1024 + 1)
    except HTTPError as error:
        status, _ = provider_failure_status(error)
        raise _Unavailable(status) from None
    except OSError, HTTPException:
        raise _Unavailable("unavailable") from None
    if len(raw) > 16 * 1024:
        raise _Unavailable("output_limit")
    try:
        return decode_json(raw), len(raw)
    except ValueError, RecursionError:
        raise _Unavailable("invalid_response") from None


def _ordering(rows, count, index_key):
    if not isinstance(rows, list) or len(rows) != count:
        raise _Unavailable("invalid_response")
    scores = {}
    for row in rows:
        if not isinstance(row, dict):
            raise _Unavailable("invalid_response")
        index, score = row.get(index_key), row.get("score")
        if (
            type(index) is not int
            or not 0 <= index < count
            or index in scores
            or type(score) not in (float, int)
            or not 0 <= score <= 1
            or not math.isfinite(score)
        ):
            raise _Unavailable("invalid_response")
        scores[index] = score
    return sorted(range(count), key=lambda index: -scores[index])


def _local_order(scope, query, texts, check):
    config = settings.PANDECT_LOCAL_RERANK
    origin = config["url"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    if config["token"]:
        headers["Authorization"] = "Bearer " + config["token"]

    def verify_runtime():
        check()
        info, _ = _read(Request(origin + "/info", headers=headers), local=True)
        if not (
            isinstance(info, dict)
            and info.get("model_id") == scope["model"]
            and info.get("model_sha") == scope["model_revision"]
            and info.get("version") == scope["runtime_version"]
            and isinstance(info.get("model_type"), dict)
            and "reranker" in info["model_type"]
        ):
            raise _Unavailable("model_changed")

    verify_runtime()
    body = encode_json(
        {
            "query": query,
            "texts": texts,
            "raw_scores": False,
            "return_text": False,
            "truncate": True,
        }
    )
    check()
    result, _ = _read(
        Request(origin + "/rerank", data=body, headers=headers), local=True
    )
    verify_runtime()
    return _ordering(result, len(texts), "index")


def _hosted_order(case, scope, query, texts, check, chat_turn):
    # Workers AI's native gateway route applies separate gateway and upstream
    # authentication and uses the application's payload-logging/cache policy.
    url, headers = endpoint_and_headers(scope)
    body = encode_json(
        {"query": query, "contexts": [{"text": t} for t in texts], "top_k": len(texts)}
    )
    request = Request(url, data=body, headers=headers)
    check()
    receipt = GatewayRequest.objects.create(
        case=case,
        chat_turn=chat_turn,
        operation=GatewayRequest.Operation.EVIDENCE_CHAT
        if chat_turn
        else GatewayRequest.Operation.SEARCH_QUERY,
        model=scope["model"],
        request_bytes=len(body),
    )
    status, response_bytes = "cancelled", 0
    try:
        check()
        data, response_bytes = _read(request, local=False)
        if not isinstance(data, dict) or data.get("success") is False:
            raise _Unavailable("invalid_response")
        result = data.get("result", data)
        rows = result.get("response") if isinstance(result, dict) else None
        order = _ordering(rows, len(texts), "id")
        check()
        status = "complete"
        return order
    except _Unavailable as exc:
        status = str(exc)
        raise
    finally:
        GatewayRequest.objects.filter(pk=receipt.pk).update(
            status=status, response_bytes=response_bytes, output_tokens=0
        )


def competing_positions(texts):
    """Select close alternatives to the lead; independent sources keep their rank."""
    words = [set(re.findall(r"[^\W_]{4,}", text.lower())) for text in texts]
    lead = words[0]
    positions = [0]
    for index, other in enumerate(words[1:], 1):
        common = len(lead & other)
        if common >= 3 and common >= 0.6 * min(len(lead), len(other)):
            positions.append(index)
    return positions


def rerank_order(case, query, texts, *, scope, authorize, chat_turn=None):
    """Return positions in relevance order. Caller revalidates all input sources.

    A delivery may die between the reservation and the response. A resumed turn
    retains its combined rank in that case, avoiding a duplicate transfer.
    """
    original = list(range(len(texts)))
    if not scope or len(texts) < 2 or not query.strip() or scope != rerank_scope(case):
        return original
    if chat_turn and (
        chat_turn.case_id != case.pk or chat_turn.plan.get("rerank_scope") != scope
    ):
        return original
    texts = [t[:MAX_PASSAGE_CHARS] for t in texts[:MAX_CANDIDATES]]
    positions = competing_positions(texts)
    if len(positions) < 2:
        return original
    texts = [texts[i] for i in positions]
    query = query.strip()[:200]

    def check():
        current = authorize()
        if current.pk != case.pk:
            raise AnalysisError("access_removed")
        if rerank_scope(current) != scope:
            raise _Unavailable("approval_changed")

    try:
        check()
        if chat_turn and not ChatTurn.objects.filter(
            pk=chat_turn.pk,
            case=case,
            chat__case=case,
            status="running",
            delivery_token=chat_turn.delivery_token,
            rerank_started_at__isnull=True,
        ).update(rerank_started_at=timezone.now()):
            return original
        if scope["provider"] == "local":
            order = _local_order(scope, query, texts, check)
        else:
            order = _hosted_order(case, scope, query, texts, check, chat_turn)
        check()
        for slot, index in zip(positions, order, strict=True):
            original[slot] = positions[index]
        return original
    except _Unavailable:
        # A transport failure must never mask a changed source or lost access.
        if authorize().pk != case.pk:
            raise AnalysisError("access_removed") from None
        return original

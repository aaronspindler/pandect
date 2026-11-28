"""Bounded Cloudflare AI Search REST calls. URLs and item keys are server-owned."""

import json
import re
from concurrent.futures import Future
from contextlib import nullcontext
from functools import partial
from http.client import HTTPException
from threading import BoundedSemaphore, Thread
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cases.models import Case
from core.analysis.http import NoRedirect
from core.models import GatewayRequest

from . import rate_limit

MAX_RESPONSE = 1024 * 1024
MATCH_THRESHOLD = 0.4
_deadline_slots = BoundedSemaphore(8)


def instance_settings(keyword_match_mode):
    """Creation settings for an upload-only index of Pandect text receipts."""
    return {
        "public_endpoint_params": {"enabled": False},
        "embedding_model": "@cf/qwen/qwen3-embedding-0.6b",
        "index_method": {"vector": True, "keyword": True},
        "indexing_options": {
            "keyword_tokenizer": "porter",
            "file_summary": False,
            "use_ocr": False,
        },
        "chunk": True,
        "chunk_size": 256,
        "chunk_overlap": 10,
        "fusion_method": "rrf",
        "retrieval_options": {"keyword_match_mode": keyword_match_mode, "boost_by": []},
        "score_threshold": MATCH_THRESHOLD,
        "max_num_results": 10,
        "cache": False,
        "rewrite_query": False,
        "reranking": False,
        "custom_metadata": [
            {"field_name": key, "data_type": value}
            for key, value in (
                ("pub", "number"),
                ("from_at", "number"),
                ("to_at", "number"),
                ("kind", "text"),
                ("surface", "text"),
            )
        ],
    }


class HostedSearchError(Exception):
    def __init__(self, code, *, retry_at=None, provider_throttled=False):
        self.code = code
        self.retry_at = retry_at
        self.provider_throttled = provider_throttled
        super().__init__(code)


def destination():
    config = settings.PANDECT_CLOUDFLARE_SEARCH
    return {key: config.get(key, "") for key in ("account", "namespace")}


def check_deadline(deadline):
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise HostedSearchError("search_deadline")
    return remaining


def request(
    index,
    method,
    path="",
    *,
    payload=None,
    file=None,
    collection=False,
    namespace=False,
    missing_ok=False,
    allow_conflict=False,
    authorize_transfer=None,
    usage_callback=None,
    deadline=None,
    _budget=None,
):
    check_deadline(deadline)
    config = settings.PANDECT_CLOUDFLARE_SEARCH
    if (
        not config.get("token")
        or config.get("account") != index.account
        or not re.fullmatch(r"[0-9a-fA-F]{32}", index.account)
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,26}[a-z0-9])?", index.namespace)
    ):
        raise HostedSearchError("search_configuration")
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{index.account}"
        f"/ai-search/namespaces/{index.namespace}"
    )
    if not namespace:
        url += "/instances"
    if not (namespace or collection):
        url += "/" + index.instance
    track = method == "POST" and path in ("/search", "/items")
    headers = {
        "Authorization": "Bearer " + config["token"],
        "cf-aig-collect-log": "false",
        "cf-aig-collect-log-payload": "false",
        "cf-aig-skip-cache": "true",
    }
    body = None
    if file:
        filename, content, metadata = file
        boundary = uuid4().hex
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
            f"{content}\r\n--{boundary}\r\nContent-Disposition: form-data; "
            f'name="metadata"\r\n\r\n{json.dumps(metadata)}\r\n--{boundary}--\r\n'
        ).encode()
    elif payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    timing = {"deadline": deadline} if deadline is not None else {}
    budget_scope = nullcontext(_budget) if _budget else rate_limit.budget(index.account)
    with budget_scope as budget:
        if _budget is None and (retry_at := budget.reserve(3 if track else 1)):
            raise HostedSearchError("search_rate_limit", retry_at=retry_at)
        if track:
            # Public routing can change on either resource after provisioning.
            private_namespace(index, _budget=budget, **timing)
            private_instance(index, request(index, "GET", _budget=budget, **timing))
        if (_budget or track) and (retry_at := budget.cooldown()):
            raise HostedSearchError("search_rate_limit", retry_at=retry_at)
        check_deadline(deadline)
        if track:
            authorize_transfer()

        outcome = "unavailable"
        response_bytes = 0
        try:
            result, response_bytes = exchange(
                Request(url + path, data=body, method=method, headers=headers),
                missing_ok,
                allow_conflict,
                **timing,
            )
            outcome = "complete"
            return result
        except HostedSearchError as exc:
            outcome = exc.code
            if exc.retry_at is not None:
                exc.retry_at = budget.throttle(exc.retry_at)
            raise
        finally:
            if track:
                operation = "search_query" if path == "/search" else "search_index"
                register_usage = usage_callback or transaction.on_commit
                register_usage(
                    partial(
                        record_usage,
                        index.case_key,
                        operation,
                        outcome,
                        len(body or b""),
                        response_bytes,
                    )
                )


@transaction.atomic
def record_usage(case_id, operation, status, sent, received):
    case = Case.objects.select_for_update(no_key=True).filter(pk=case_id).first()
    if case:
        GatewayRequest.objects.create(
            case=case,
            operation=operation,
            model="cloudflare-ai-search",
            status=status,
            request_bytes=sent,
            response_bytes=received,
        )


def exchange(req, missing_ok, allow_conflict, *, deadline=None):
    if deadline is None:
        return _exchange(req, missing_ok, allow_conflict, deadline)
    check_deadline(deadline)
    if not _deadline_slots.acquire(blocking=False):
        raise HostedSearchError("search_unavailable")
    result = Future()

    def transport():
        # urllib's socket timeout is an inactivity limit. Keep a stalled header,
        # body or DNS read off the page's critical path without queueing requests.
        try:
            result.set_result(_exchange(req, missing_ok, allow_conflict, deadline))
        except Exception as exc:
            result.set_exception(exc)
        finally:
            _deadline_slots.release()

    try:
        Thread(target=transport, daemon=True, name="cloudflare-search").start()
    except RuntimeError:
        _deadline_slots.release()
        raise HostedSearchError("search_unavailable") from None
    try:
        value = result.result(timeout=check_deadline(deadline))
    except TimeoutError:
        raise HostedSearchError("search_deadline") from None
    check_deadline(deadline)
    return value


def _exchange(req, missing_ok, allow_conflict, deadline):
    remaining = check_deadline(deadline)
    timeout = 15 if remaining is None else min(15, remaining)
    try:
        with build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            if deadline is None:
                raw = response.read(MAX_RESPONSE + 1)
            else:
                raw = bytearray()
                while len(raw) <= MAX_RESPONSE:
                    check_deadline(deadline)
                    chunk = response.read1(min(65536, MAX_RESPONSE + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                check_deadline(deadline)
    except HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        exc.close()
        if exc.code == 429:
            raise HostedSearchError(
                "search_rate_limit",
                retry_at=rate_limit.retry_deadline(retry_after, timezone.now()),
                provider_throttled=True,
            ) from None
        if missing_ok and exc.code == 404:
            return None, 0
        if allow_conflict and exc.code == 409:
            return None, 0
        raise HostedSearchError("search_unavailable") from None
    except OSError, HTTPException:
        check_deadline(deadline)
        raise HostedSearchError("search_unavailable") from None
    if len(raw) > MAX_RESPONSE:
        raise HostedSearchError("search_response_invalid")
    if not raw and req.get_method() == "DELETE":
        return None, 0
    try:
        value = json.loads(raw)
    except ValueError, RecursionError:
        raise HostedSearchError("search_response_invalid") from None
    if not isinstance(value, dict) or value.get("success") is not True:
        raise HostedSearchError("search_response_invalid")
    return value.get("result"), len(raw)


def private_namespace(index, *, _budget=None, deadline=None):
    timing = {"deadline": deadline} if deadline is not None else {}
    namespace = request(index, "GET", namespace=True, _budget=_budget, **timing)
    if not isinstance(namespace, dict) or namespace.get("name") != index.namespace:
        raise HostedSearchError("search_instance_private")
    endpoint = namespace.get("public_endpoint_params")
    if endpoint is not None and (
        not isinstance(endpoint, dict) or endpoint.get("enabled") is not False
    ):
        raise HostedSearchError("search_instance_private")


def private_instance(index, instance):
    if (
        not isinstance(instance, dict)
        or instance.get("id") != index.instance
        or not isinstance(instance.get("public_endpoint_params"), dict)
        or instance["public_endpoint_params"].get("enabled") is not False
    ):
        raise HostedSearchError("search_instance_private")


def ensure_instance(index, authorize):
    authorize()
    private_namespace(index)
    authorize()
    instance = request(index, "GET", missing_ok=True)
    if instance is None:
        authorize()
        instance = request(
            index,
            "POST",
            collection=True,
            allow_conflict=True,
            payload={
                "id": index.instance,
                **instance_settings(index.keyword_match_mode),
            },
        )
        if instance is None:
            authorize()
            instance = request(index, "GET")
    private_instance(index, instance)


def item_path(remote_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", remote_id):
        raise HostedSearchError("search_response_invalid")
    return "/items/" + quote(remote_id, safe="")

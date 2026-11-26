"""Bounded Cloudflare AI Search REST calls. URLs and item keys are server-owned."""

import json
import re
from functools import partial
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener
from uuid import uuid4

from django.conf import settings
from django.db import transaction

from cases.models import Case
from core.analysis.http import NoRedirect
from core.models import GatewayRequest

MAX_RESPONSE = 1024 * 1024


class HostedSearchError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def destination():
    config = settings.PANDECT_CLOUDFLARE_SEARCH
    return {key: config.get(key, "") for key in ("account", "namespace")}


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
):
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
    if track:
        # An operator can change public routing on either resource after provisioning.
        private_namespace(index)
        private_instance(index, request(index, "GET"))
        authorize_transfer()
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
    outcome = "unavailable"
    response_bytes = 0
    try:
        result, response_bytes = exchange(
            Request(url + path, data=body, method=method, headers=headers),
            missing_ok,
            allow_conflict,
        )
        outcome = "complete"
        return result
    except HostedSearchError as exc:
        outcome = exc.code
        raise
    finally:
        if track:
            operation = "search_query" if path == "/search" else "search_index"
            transaction.on_commit(
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


def exchange(req, missing_ok, allow_conflict):
    try:
        with build_opener(NoRedirect()).open(req, timeout=15) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except HTTPError as exc:
        exc.close()
        if missing_ok and exc.code == 404:
            return None, 0
        if allow_conflict and exc.code == 409:
            return None, 0
        raise HostedSearchError("search_unavailable") from None
    except OSError, HTTPException:
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


def private_namespace(index):
    namespace = request(index, "GET", namespace=True)
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
                "public_endpoint_params": {"enabled": False},
                "embedding_model": "@cf/qwen/qwen3-embedding-0.6b",
                "index_method": {"vector": True, "keyword": True},
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
                "cache": False,
                "rewrite_query": False,
                "reranking": False,
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

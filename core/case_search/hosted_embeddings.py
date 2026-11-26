"""Bounded OpenAI embedding requests using the approved Cloudflare gateway."""

from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from django.core.exceptions import PermissionDenied
from django.http import Http404

from core.analysis import chat_usage
from core.analysis.errors import AnalysisError
from core.analysis.gateway import endpoint_and_headers
from core.analysis.http import NoRedirect, provider_failure_status
from core.analysis.inference_payloads import decode_json, encode_json
from core.models import GatewayRequest

from .semantic_model import CHUNK_BYTES, HOSTED_DIMENSIONS, valid_vector


def _vectors(result, count, model):
    if not isinstance(result, dict) or result.get("model") != model:
        raise AnalysisError("gateway_invalid_response")
    rows = result.get("data")
    if not isinstance(rows, list) or len(rows) != count:
        raise AnalysisError("gateway_invalid_response")
    vectors = [None] * count
    for row in rows:
        index = row.get("index") if isinstance(row, dict) else None
        if (
            type(index) is not int
            or not 0 <= index < count
            or vectors[index] is not None
            or not valid_vector(row.get("embedding"), HOSTED_DIMENSIONS)
        ):
            raise AnalysisError("gateway_invalid_response")
        vectors[index] = row["embedding"]
    return vectors


def request_embeddings(texts, config, authorize, case, purpose, chat_turn=None):
    if purpose not in ("index", "query"):
        raise AnalysisError("gateway_invalid_input")
    if (
        any(not isinstance(text, str) or not text for text in texts)
        or purpose == "query"
        and (len(texts) != 1 or len(texts[0]) > 200)
        or purpose == "index"
        and any(len(text.encode()) > CHUNK_BYTES for text in texts)
    ):
        raise AnalysisError("gateway_input_limit")
    body = encode_json(
        {
            "model": config["model"],
            "dimensions": HOSTED_DIMENSIONS,
            "encoding_format": "float",
            "input": texts,
        }
    )
    if len(body) > config["max_request_bytes"]:
        raise AnalysisError("gateway_input_limit")
    url, headers = endpoint_and_headers(config)
    request = Request(url, data=body, headers=headers, method="POST")
    authorize()
    reservation = chat_usage.reserve(chat_turn, "embedding", len(body))
    usage = GatewayRequest.objects.create(
        case=case,
        chat_turn=chat_turn,
        operation=GatewayRequest.Operation.EVIDENCE_CHAT
        if chat_turn
        else f"search_{purpose}",
        model=config["model"],
        request_bytes=len(body),
    )
    update = {"status": "cancelled"}
    try:
        authorize()
        try:
            with build_opener(NoRedirect()).open(
                request, timeout=config["timeout"]
            ) as response:
                raw = response.read(config["max_response_bytes"] + 1)
        except HTTPError as exc:
            status, update["response_bytes"] = provider_failure_status(exc)
            if exc.code == 408 or 500 <= exc.code < 600:
                status = "unavailable"
            raise AnalysisError("gateway_" + status) from None
        update["response_bytes"] = len(raw)
        if len(raw) > config["max_response_bytes"]:
            raise AnalysisError("gateway_output_limit")
        result = decode_json(raw)
        vectors = _vectors(result, len(texts), config["model"])
        tokens = result.get("usage")
        if isinstance(tokens, dict):
            prompt_tokens = tokens.get("prompt_tokens")
            if type(prompt_tokens) is int and 0 <= prompt_tokens < 2**31:
                update["input_tokens"] = prompt_tokens
        update["output_tokens"] = 0
        if reservation:
            reservation.settle(update.get("input_tokens"), 0)
        authorize()
        update["status"] = "complete"
        return vectors
    except PermissionDenied, Http404:
        update["status"] = "access_removed"
        raise
    except AnalysisError as exc:
        if exc.code.startswith("gateway_") or exc.code == "access_removed":
            update["status"] = exc.code.removeprefix("gateway_")
        raise
    except OSError, HTTPException:
        update["status"] = "unavailable"
        raise AnalysisError("gateway_unavailable") from None
    except ValueError, RecursionError:
        update["status"] = "invalid_response"
        raise AnalysisError("gateway_invalid_response") from None
    finally:
        GatewayRequest.objects.filter(pk=usage.pk).update(**update)

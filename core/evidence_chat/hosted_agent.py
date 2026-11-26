"""Dispatch an opaque work item and authorize the Agent's single-delivery callbacks."""

import json
from http.client import HTTPException
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from django.conf import settings
from django.core import signing
from django.core.exceptions import PermissionDenied, RequestDataTooBig
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.analysis.errors import AnalysisError
from core.analysis.http import NoRedirect
from core.models import ChatTurn

from .checkpoints import authorize_turn

SALT = "pandect.investigation.delivery.v1"
MAX_BODY = 96 * 1024


def agent_config():
    config = settings.PANDECT_CLOUDFLARE_AGENT
    url = urlsplit(config["url"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != "/step"
        or len(config["secret"]) < 32
    ):
        raise AnalysisError("chat_agent_not_configured")
    return config


def capability(turn):
    return signing.dumps(
        [
            str(turn.case_id),
            str(turn.pk),
            str(turn.delivery_token),
            turn.approval_revision,
        ],
        salt=SALT,
    )


def dispatch(turn):
    if turn.plan.get("agent_version") != 1:
        raise AnalysisError("chat_agent_cutover")
    case = authorize_turn(turn)
    config = agent_config()
    if case.external_analysis_approval.get("agent_url") != config["url"]:
        raise AnalysisError("execution_changed")
    body = json.dumps(
        {
            "case": str(case.pk),
            "turn": str(turn.pk),
            "stage": turn.plan.get("stage", "start"),
            "capability": capability(turn),
        }
    ).encode()
    request = Request(
        config["url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config["secret"],
            "User-Agent": "Pandect/0.1",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=360) as response:
            if response.status != 204:
                raise AnalysisError("chat_agent_unavailable")
    except URLError, OSError, HTTPException:
        raise AnalysisError("chat_agent_unavailable") from None
    # An acknowledgement alone never completes a question.
    if ChatTurn.objects.filter(
        pk=turn.pk, delivery_token=turn.delivery_token, status="running"
    ).exists():
        raise AnalysisError("chat_agent_unavailable")


@transaction.atomic
def _claim(case_id, turn_id, token, revision, operation):
    turn = (
        ChatTurn.objects.select_related("case", "asked_by")
        .filter(
            pk=turn_id,
            case_id=case_id,
            chat__case_id=case_id,
            status="running",
            delivery_token=token,
            approval_revision=revision,
        )
        .first()
    )
    if turn is None or turn.plan.get("agent_version") != 1:
        raise Http404
    # Lock order follows submission, evidence deletion, and owner revocation.
    authorize_turn(turn, lock=True)
    turn = (
        ChatTurn.objects.select_for_update(of=("self",))
        .select_related("case", "asked_by")
        .get(pk=turn.pk)
    )
    if str(turn.delivery_token) != token or turn.status != "running":
        raise Http404
    needed = turn.plan.get("stage", "start")
    if needed == "decide":
        needed = "act" if turn.plan.get("agent_decision") else "model"
    if (
        needed == "review"
        and turn.plan.get("literal")
        and turn.plan.get("agent_callback")
        in ([token, "review"], [token, "literal_result"])
    ):
        needed = "literal_result"
    called = [token, operation]
    if operation != needed or turn.plan.get("agent_callback") == called:
        raise Http404
    turn.plan["agent_callback"] = called
    turn.save(update_fields=["plan"])
    return turn


@csrf_exempt
@require_POST
def callback(request, case_id, turn_id):
    # Browser cookies cannot authorize the endpoint. Only Django mints this bearer.
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 2048:
        return HttpResponse(status=403)
    try:
        case, identifier, token, revision = signing.loads(
            auth[7:], salt=SALT, max_age=480
        )
        if case != str(case_id) or identifier != str(turn_id):
            return HttpResponse(status=403)
        if (
            request.content_type != "application/json"
            or not 0 < int(request.META.get("CONTENT_LENGTH") or 0) <= MAX_BODY
        ):
            return HttpResponse(status=413)
        body = json.loads(request.body)
        if not isinstance(body, dict) or not isinstance(body.get("operation"), str):
            return HttpResponse(status=400)
    except (
        signing.BadSignature,
        TypeError,
        ValueError,
        RecursionError,
        RequestDataTooBig,
    ):
        return HttpResponse(status=403)
    try:
        turn = _claim(case_id, turn_id, token, revision, body["operation"])
    except Http404, PermissionDenied, AnalysisError:
        return HttpResponse(status=403)
    from .investigation import step
    from .jobs import _fail

    try:
        response = step(turn, body)
        return JsonResponse(response or {}, headers={"Cache-Control": "no-store"})
    except Http404, PermissionDenied:
        _fail(turn, turn.delivery_token, "access_removed")
    except AnalysisError as exc:
        _fail(turn, turn.delivery_token, exc.code)
    return HttpResponse(status=409)

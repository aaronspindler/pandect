"""Claim Evidence Chat deliveries, dispatch their saved stage and record failures."""

import logging
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from core.analysis.errors import AnalysisError
from core.models import ChatTurn

from .hosted_agent import dispatch

logger = logging.getLogger(__name__)


@transaction.atomic
def _claim(turn_id, token):
    turn = (
        ChatTurn.objects.select_for_update(of=("self",))
        .select_related("case", "asked_by")
        .filter(pk=turn_id, delivery_token=token, status__in=("queued", "running"))
        .first()
    )
    if turn is not None:
        turn.delivery_token = uuid4()
        turn.status = "running"
        turn.started_at = turn.started_at or turn.created_at
        turn.plan.pop("agent_decision", None)
        turn.save(update_fields=("delivery_token", "status", "started_at", "plan"))
    return turn


def process_turn(turn_id, token):
    turn = _claim(turn_id, token)
    if turn is None:
        return
    active_token = turn.delivery_token
    try:
        dispatch(turn)
    except Http404, PermissionDenied:
        _fail(turn, active_token, "access_removed")
    except AnalysisError as error:
        _fail(turn, active_token, error.code)
    except Exception as error:
        # Preserve a retryable domain record without retaining provider output
        # or exception text that could contain case evidence.
        _fail(turn, active_token, "processing_failed")
        logger.error(
            "Chat review failed turn=%s exception_type=%s",
            turn.pk,
            type(error).__name__,
        )


@transaction.atomic
def _fail(turn, active_token, code):
    from cases.models import Case

    from .proposals import resume_idle_chat

    Case.objects.select_for_update().filter(pk=turn.case_id).first()
    updated = ChatTurn.objects.filter(
        pk=turn.pk, delivery_token=active_token, status="running"
    ).update(
        status="failed",
        error_code=code,
        completed_at=timezone.now(),
        activity={
            key: value for key, value in turn.activity.items() if key != "sources"
        },
    )
    if updated:
        resume_idle_chat(turn.case_id, turn.chat_id)

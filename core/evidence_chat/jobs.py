"""Claim Evidence Chat deliveries, dispatch their saved stage and record failures."""

import logging
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.utils import timezone

from core.analysis.errors import AnalysisError
from core.models import ChatTurn

from . import engine
from .answer_jobs import finish_answer, refine_answer, start_answer
from .checkpoints import authorize_turn
from .full_review import freeze_review, review_batch, verify_batch

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
        turn.started_at = turn.started_at or timezone.now()
        turn.save(update_fields=("delivery_token", "status", "started_at"))
    return turn


def _run_stage(turn):
    try:
        authorize_turn(turn, review=turn.plan.get("stage") == "review")
        if turn.plan.get("stage") == "prepare_deeper_review" and turn.review_of_id:
            freeze_review(turn, turn.question, {}, narrative=True)
        elif turn.plan.get("stage") == "approved_extraction":
            from .proposals import finish_continuation

            finish_continuation(turn)
        elif turn.plan.get("stage") == "verify":
            verify_batch(turn)
        elif turn.plan.get("stage") == "review":
            if (
                turn.review_of_id
                and turn.review_requests >= turn.plan["deep_scope"]["limit"]
            ):
                raise AnalysisError("chat_deeper_review_limit")
            review_batch(turn)
        elif turn.plan.get("stage") == "refine":
            refine_answer(turn)
        else:
            start_answer(turn)
    except AnalysisError as error:
        if error.code == "chat_deeper_review_limit" and turn.review_of_id:
            from .deep_review import finish_at_limit

            finish_at_limit(turn)
            return
        if error.code != "chat_budget_exhausted" or turn.mode == "full":
            raise
        # A direct query or linked extraction can hit its limit before producing
        # any verified claims. Discard unverified selection and proposal state.
        turn.plan.pop("recorded_selection", None)
        turn.plan.pop("proposed_extraction", None)
        finish_answer(
            turn,
            engine.empty_answer(
                "Processing limit reached before this question could be verified."
            ),
            [],
        )


def process_turn(turn_id, token):
    turn = _claim(turn_id, token)
    if turn is None:
        return
    active_token = turn.delivery_token
    try:
        _run_stage(turn)
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

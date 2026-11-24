"""Inspect one chat delivery or replace a confirmed interrupted worker."""

from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.evidence_chat import ai, jobs
from core.evidence_chat.services import _edit_case
from core.models import ChatTurn


class Command(BaseCommand):
    help = "Inspect a chat delivery or resume it after confirming its worker stopped."

    def add_arguments(self, parser):
        parser.add_argument("turn_id", type=UUID)
        parser.add_argument("--confirm-interrupted", type=UUID, metavar="TOKEN")

    @transaction.atomic
    def handle(self, *, turn_id, confirm_interrupted, **options):
        try:
            turn = ChatTurn.objects.select_related("asked_by").get(pk=turn_id)
            if confirm_interrupted:
                case = _edit_case(turn.asked_by, turn.case_id)
                turn = ChatTurn.objects.select_for_update().get(
                    pk=turn_id, case=case, chat__case=case
                )
                if (
                    turn.status not in {"queued", "running"}
                    or turn.delivery_token != confirm_interrupted
                ):
                    raise CommandError("This delivery ended or its token changed.")
                ai.check_execution(turn, case)
                turn.delivery_token = uuid4()
                turn.save(update_fields=["delivery_token"])
                jobs.enqueue_turn(turn)
                return "Replacement delivery queued."
        except ChatTurn.DoesNotExist, Http404, PermissionDenied, AnalysisError:
            raise CommandError(
                "The delivery is unavailable or no longer authorized."
            ) from None
        status = f"{turn.get_status_display()} · {turn.reviewed} of {turn.total}."
        if turn.status in {"queued", "running"}:
            status += f" Delivery token: {turn.delivery_token}"
        return status

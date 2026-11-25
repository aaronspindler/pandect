"""Inspect one chat delivery or replace a confirmed interrupted worker."""

from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from core.analysis.errors import AnalysisError
from core.evidence_chat.services import recover_interrupted_delivery
from core.models import ChatTurn


class Command(BaseCommand):
    help = "Inspect a chat delivery or resume it after confirming its worker stopped."

    def add_arguments(self, parser):
        parser.add_argument("turn_id", type=UUID)
        parser.add_argument("--confirm-interrupted", type=UUID, metavar="TOKEN")

    def handle(self, *, turn_id, confirm_interrupted, **options):
        try:
            if confirm_interrupted:
                recover_interrupted_delivery(turn_id, confirm_interrupted)
                return "Replacement delivery queued."
            turn = ChatTurn.objects.get(pk=turn_id)
        except (
            ChatTurn.DoesNotExist,
            Http404,
            PermissionDenied,
            AnalysisError,
        ) as error:
            if (
                isinstance(error, AnalysisError)
                and error.code == "chat_delivery_changed"
            ):
                raise CommandError(
                    "This delivery ended or its token changed."
                ) from None
            raise CommandError(
                "The delivery is unavailable or no longer authorized."
            ) from None
        status = f"{turn.get_status_display()} · {turn.reviewed} of {turn.total}."
        if turn.status in {"queued", "running"}:
            status += f" Delivery token: {turn.delivery_token}"
        return status

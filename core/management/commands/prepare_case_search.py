from uuid import UUID

from allauth.account.models import EmailAddress
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404

from cases.models import Case
from core.case_search.indexing import prepare_case_search, search_index_status
from core.case_search.parent_index import prepare_parents
from core.case_search.semantic_model import profile


class Command(BaseCommand):
    help = "Queue meaning search for eligible cases or inspect one case's delivery."

    def add_arguments(self, parser):
        parser.add_argument(
            "--case", dest="case_id", type=UUID, help="Limit to this case ID."
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--retry", action="store_true", help="Resume failed work.")
        mode.add_argument(
            "--status",
            action="store_true",
            help="Show the selected case's status and delivery token.",
        )
        mode.add_argument(
            "--confirm-interrupted",
            type=UUID,
            metavar="TOKEN",
            help="Replace this delivery after confirming its worker stopped.",
        )

    def handle(self, *, case_id, retry, status, confirm_interrupted, **options):
        if (status or confirm_interrupted) and not case_id:
            raise CommandError("Select one case with --case.")
        cases = Case.objects.select_related("owner")
        if case_id:
            cases = cases.filter(pk=case_id)
            case = cases.first()
            if case is None:
                raise CommandError("Case not found.")
            if status:
                state = search_index_status(case)
                self.stdout.write(
                    f"{state['label']} {state['documents']} complete sources, "
                    f"{state['passages']} indexed passages."
                )
                if state["reason"]:
                    self.stdout.write(state["reason"])
                if state["status"] in {"queued", "running"}:
                    self.stdout.write(f"Delivery token: {state['token']}")
                return
        owners = EmailAddress.objects.filter(
            verified=True, user__is_active=True
        ).values("user_id")
        queued = 0
        parent_scans = 0
        for case in cases.filter(owner__in=owners).iterator(chunk_size=250):
            try:
                if not confirm_interrupted:
                    parent_scans += prepare_parents(case, case.owner, retry=retry)
                if not profile() and not confirm_interrupted:
                    continue
                queued += prepare_case_search(
                    case, case.owner, retry=retry, interrupted_token=confirm_interrupted
                )
            except Http404, PermissionDenied:
                continue
        if confirm_interrupted and not queued:
            raise CommandError(
                "The delivery token is stale or the case is ineligible for recovery."
            )
        self.stdout.write(
            f"Queued {queued} case search indexes. "
            f"Queued {parent_scans} local outline scans."
        )

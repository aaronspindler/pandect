from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cases.models import Case
from core.case_search.indexing import prepare_case_search, search_index_status
from core.case_search.lifecycle import enqueue
from core.models import HostedSearchIndex


class Command(BaseCommand):
    help = "Reconcile approved Cloudflare indexes or retry retired instance cleanup."

    def add_arguments(self, parser):
        parser.add_argument("--case", dest="case_id", type=UUID)
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--retry", action="store_true")
        parser.add_argument("--confirm-interrupted", type=UUID, metavar="TOKEN")
        parser.add_argument("--cleanup", action="store_true")

    def handle(
        self, *, case_id, status, retry, confirm_interrupted, cleanup, **options
    ):
        if cleanup:
            with transaction.atomic():
                retired = HostedSearchIndex.objects.select_for_update().filter(
                    retired=True
                )
                if case_id:
                    retired = retired.filter(case_key=case_id)
                count = 0
                for index in retired:
                    index.failures = 0
                    enqueue(index)
                    count += 1
            self.stdout.write(f"Queued {count} private instance cleanup deliveries.")
            return
        if (status or confirm_interrupted) and not case_id:
            raise CommandError("Select a case with --case.")
        cases = Case.objects.filter(external_ai_enabled=True).select_related("owner")
        if case_id:
            cases = cases.filter(pk=case_id)
        if status:
            for receipt in HostedSearchIndex.objects.filter(
                case_key=case_id, retired=True
            ):
                self.stdout.write(
                    f"Private instance cleanup {receipt.status}. {receipt.error_code}"
                )
            for case in cases:
                state = search_index_status(case)
                self.stdout.write(
                    f"{state['label']} Complete passages: {state['passages']}. "
                    f"Delivery: {state['token']}"
                )
            return
        count = sum(
            prepare_case_search(
                case, case.owner, retry=retry, interrupted_token=confirm_interrupted
            )
            for case in cases.iterator()
        )
        self.stdout.write(f"Queued {count} private search indexes.")

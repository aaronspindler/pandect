from allauth.account.models import EmailAddress
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand
from django.http import Http404

from cases.models import Case
from core.case_search.indexing import prepare_case_search
from core.case_search.semantic_model import profile


class Command(BaseCommand):
    help = "Queue private semantic indexing for all eligible cases."

    def add_arguments(self, parser):
        parser.add_argument(
            "--retry", action="store_true", help="Resume interrupted or failed work."
        )

    def handle(self, *, retry, **options):
        if not profile():
            self.stdout.write("Meaning search is not configured.")
            return
        owners = EmailAddress.objects.filter(
            verified=True, user__is_active=True
        ).values("user_id")
        cases = Case.objects.filter(owner__in=owners).select_related("owner")
        queued = 0
        for case in cases.iterator(chunk_size=250):
            try:
                queued += prepare_case_search(case, case.owner, retry=retry)
            except Http404, PermissionDenied:
                continue
        self.stdout.write(f"Queued {queued} case search indexes.")

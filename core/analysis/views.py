"""Case analysis landing page and latest records."""

from django.http import QueryDict
from django.urls import reverse
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.findings import case_highlights
from core.analysis.operations import CASE_OPERATIONS
from core.analysis.submission_views import operation_groups
from core.evidence.collection import collection_context
from socialmedia import instagram_connector
from socialmedia.models import InstagramMonitor
from ui.live import render_live


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    context = case_context(case, request.user) | {"nav_section": "analysis"}
    evidence = collection_context(case, QueryDict())["rows"][:3]
    latest_records = [row | {"record_type": "Evidence"} for row in evidence]
    latest_records.extend(case_highlights(case))
    latest_records.sort(key=lambda row: row["created_at"], reverse=True)
    return render_live(
        request,
        "core/analysis/index.html",
        context
        | {
            "latest_records": latest_records,
            "operation_groups": operation_groups(
                CASE_OPERATIONS,
                {},
                base_url=reverse("core:analysis:create", args=[case.pk]),
            )
            if context["can_edit"]
            else [],
            "show_instagram_monitors": instagram_connector.scheduling_available()
            or InstagramMonitor.objects.filter(case=case).exists(),
            "reports": (
                ("calls", "Call patterns"),
                ("vehicle-coverage", "Vehicle observation coverage"),
                ("vehicle-speed", "Vehicle speed and heading"),
                ("duplicates", "Exact file duplicates"),
            ),
        },
        collection=True,
    )

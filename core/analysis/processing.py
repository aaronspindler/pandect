"""Current case work and retained processing history, separate from queue rows."""

from uuid import UUID

from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import error_message
from core.analysis.forms import EvidenceAnalysisForm
from core.analysis.navigation import history_filters, query_suffix
from core.analysis.operations import OPERATIONS
from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS, KINDS
from core.models import AnalysisRun, CaseAnalysisBatch
from geo.models import VehicleLocationImport, VehicleTripGeneration


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    all_runs = AnalysisRun.objects.filter(case=case).defer("parameters", "inputs")
    all_runs = all_runs.annotate(
        first_kind=F("inputs__0__kind"),
        first_evidence=F("inputs__0__evidence_id"),
        import_phase=F("parameters__phase"),
    )
    active_runs = Paginator(
        all_runs.filter(status__in=("queued", "running")), 10
    ).get_page(request.GET.get("active_page"))
    runs = all_runs
    batch = None
    if value := request.GET.get("batch"):
        try:
            batch = CaseAnalysisBatch.objects.get(case=case, pk=UUID(value))
        except ValueError, CaseAnalysisBatch.DoesNotExist:
            raise Http404("Processing batch not found.") from None
        runs = runs.filter(batch=batch)
    filters = history_filters(request.GET)
    if "kind" in filters:
        runs = runs.filter(inputs__contains=[{"kind": filters["kind"]}])
    if "status" in filters:
        runs = runs.filter(status=filters["status"])
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    displayed_runs = [*active_runs, *page]
    titles = {}
    for kind in {run.first_kind for run in displayed_runs} & KINDS.keys():
        titles.update(
            {
                (kind, str(pk)): title
                for pk, title in scoped_evidence(case, kind)
                .filter(
                    pk__in=[
                        run.first_evidence
                        for run in displayed_runs
                        if run.first_kind == kind
                    ]
                )
                .values_list("pk", "title")
            }
        )
    for run in displayed_runs:
        operation = OPERATIONS.get(run.operation)
        run.label = operation.label if operation else "Retained analysis"
        if run.operation == "imessage_import" and run.import_phase == "preview":
            run.label = "Prepare message review"
        run.failure_message = error_message(run.error_code) if run.error_code else ""
        run.source_title = titles.get(
            (run.first_kind, run.first_evidence), "Source unavailable"
        )
    all_batches = CaseAnalysisBatch.objects.filter(case=case).defer(
        "preparation_errors"
    )
    active_batches = Paginator(all_batches.filter(status="queued"), 5).get_page(
        request.GET.get("preparing_page")
    )
    batches = Paginator(all_batches, 10).get_page(request.GET.get("batches_page"))
    imports = VehicleLocationImport.objects.filter(
        case=case, document__case=case
    ).select_related("document")
    imports = imports.only(
        "id",
        "document__title",
        "status",
        "phase",
        "created_at",
        "processed_rows",
        "row_count",
    )
    active_imports = Paginator(
        imports.filter(status__in=("pending", "queued", "running", "review")), 10
    ).get_page(request.GET.get("active_imports_page"))
    import_history = Paginator(imports, 25).get_page(request.GET.get("imports_page"))
    trip_generations = (
        VehicleTripGeneration.objects.filter(
            case=case,
            vehicle__case=case,
            state__case=case,
            state__vehicle_id=F("vehicle_id"),
        )
        .select_related("vehicle")
        .defer("cursor", "carry", "metrics")
    )
    active_trips = Paginator(
        trip_generations.filter(
            Q(status__in=("queued", "running"))
            | Q(status="complete", geocode_status__in=("queued", "running"))
        ),
        10,
    ).get_page(request.GET.get("active_trips_page"))
    trip_history = Paginator(trip_generations, 10).get_page(
        request.GET.get("trips_page")
    )
    return render(
        request,
        "core/analysis/processing.html",
        case_context(case, request.user)
        | {
            "nav_section": "processing",
            "page_obj": page,
            "active_runs": active_runs,
            "active_batches": active_batches,
            "active_imports": active_imports,
            "import_history": import_history,
            "active_trips": active_trips,
            "trip_history": trip_history,
            "batches": batches,
            "batch": batch,
            "batch_failures": [
                failure
                | {
                    "operation_label": OPERATIONS[failure["operation"]].label
                    if failure.get("operation") in OPERATIONS
                    else ""
                }
                for failure in batch.preparation_errors
            ]
            if batch
            else [],
            "history_filters": filters,
            "history_suffix": query_suffix(
                filters | ({"history_page": page.number} if page.number > 1 else {})
            ),
            "evidence_kinds": KIND_LABELS.items(),
            "statuses": AnalysisRun.Status.choices,
            "case_analysis_form": EvidenceAnalysisForm(),
        },
    )

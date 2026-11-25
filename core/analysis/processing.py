"""Current case work and retained processing history, separate from queue rows."""

from collections import Counter
from uuid import UUID, uuid4

from django.core.paginator import Paginator
from django.db.models import Count, Exists, F, OuterRef, Q, TextField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.http import Http404
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import error_message
from core.analysis.navigation import evidence_return_url, history_filters, query_suffix
from core.analysis.operations import OPERATION_GROUPS, OPERATIONS
from core.analysis.reprocessing import MODE_LABELS
from core.analysis.retries import failed_checks, latest_failed_retry
from core.case_search.indexing import search_index_status
from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS, KINDS
from core.models import AnalysisResult, AnalysisRun, CaseAnalysisBatch, ChatTurn
from geo.models import VehicleLocationImport, VehicleTripGeneration
from geo.trip_processing import latest_generations
from phone.models import CallHistoryImport
from socialmedia.models import InstagramCheck
from socialmedia.monitor_jobs import check_message
from ui.live import render_live

ACTIVE_RUNS = ("queued", "running")
ACTIVE_IMPORTS = ("queued", "running")
ACTIVE_CALL_IMPORTS = (
    CallHistoryImport.Status.PREPARING,
    CallHistoryImport.Status.PUBLISHING,
)
FAILED_CALL_IMPORTS = (
    CallHistoryImport.Status.PREPARE_FAILED,
    CallHistoryImport.Status.PUBLISH_FAILED,
)
IMPORTS_AWAITING_ACTION = ("pending", "review")
ACTIVE_TRIPS = Q(status__in=ACTIVE_RUNS) | Q(
    status="complete", geocode_status__in=ACTIVE_RUNS
)
PROCESSED_RUNS = (
    AnalysisRun.Status.COMPLETE,
    AnalysisRun.Status.PARTIAL,
    AnalysisRun.Status.EMPTY,
    AnalysisRun.Status.INSUFFICIENT,
    AnalysisRun.Status.UNSUPPORTED,
)
NEEDS_ATTENTION_LIMIT = 3


def case_imports(case):
    return VehicleLocationImport.objects.filter(case=case, document__case=case)


def case_call_imports(case):
    return CallHistoryImport.objects.filter(
        case=case,
        document__case=case,
        source__case=case,
        source_sha256=F("document__sha256"),
    )


def case_trip_generations(case):
    return VehicleTripGeneration.objects.filter(
        case=case,
        vehicle__case=case,
        state__case=case,
        state__vehicle_id=F("vehicle_id"),
    )


def _run_counts(case_runs):
    counts = (
        case_runs.values_list("operation", "status")
        .annotate(count=Count("*"))
        .order_by()
    )
    statuses = Counter()
    saved = Counter()
    methods = {method for group in OPERATION_GROUPS.values() for method in group}
    for operation, status, count in counts:
        statuses[status] += count
        if operation in methods and status in PROCESSED_RUNS:
            saved[operation] += count
    rows = [
        {"label": OPERATIONS[operation].label, "count": count}
        for operation, count in saved.items()
    ]
    return statuses, sorted(
        rows, key=lambda row: (-row["count"], row["label"].casefold())
    )


def _status_counts(work):
    return dict(work.values_list("status").annotate(count=Count("*")).order_by())


def _processing_summary(
    run_counts,
    batches,
    imports,
    call_import_counts,
    trips,
    monitor_counts,
    index_status,
):
    """Bucket each saved work record at its current processing stage."""
    counts = run_counts.copy()
    counts.update(_status_counts(batches))
    counts.update(_status_counts(imports))
    counts.update(call_import_counts)
    counts.update(monitor_counts)
    trip_counts = (
        trips.values_list("status", "geocode_status")
        .annotate(count=Count("*"))
        .order_by()
    )
    for status, geocode_status, count in trip_counts:
        if status == "complete" and geocode_status in (*ACTIVE_RUNS, "failed"):
            status = geocode_status
        counts[status] += count
    counts["complete" if index_status == "ready" else index_status] += 1
    return [
        {"label": "Queued work items", "count": counts["queued"]},
        {"label": "Processing", "count": counts["running"], "tone": "active"},
        {
            "label": "Processed",
            "count": sum(counts[status] for status in PROCESSED_RUNS),
            "tone": "success",
        },
        {"label": "Failed", "count": counts["failed"], "tone": "error"},
    ]


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    case_runs = AnalysisRun.objects.filter(case=case)
    all_runs = case_runs.defer("parameters", "inputs").annotate(
        first_kind=F("inputs__0__kind"),
        first_evidence=F("inputs__0__evidence_id"),
        second_kind=F("inputs__1__kind"),
        import_phase=F("parameters__phase"),
        execution=F("parameters___execution"),
        has_saved_result=Exists(
            AnalysisResult.objects.filter(case=case, run=OuterRef("pk"))
        ),
    )
    runs = all_runs
    batch = None
    batch_reader_url = ""
    if value := request.GET.get("batch"):
        try:
            batch = CaseAnalysisBatch.objects.get(case=case, pk=UUID(value))
        except ValueError, CaseAnalysisBatch.DoesNotExist:
            raise Http404("Processing batch not found.") from None
        runs = runs.filter(batch=batch)
        if batch.scope.get("type") == "selected_analysis":
            batch_reader_url = evidence_return_url(case.pk, request.GET)
    active = all_runs.filter(status__in=ACTIVE_RUNS)
    if batch:
        active = active.exclude(batch=batch)
    filters = history_filters(request.GET)
    if "kind" in filters:
        runs = runs.filter(inputs__contains=[{"kind": filters["kind"]}])
    if "status" in filters:
        runs = runs.filter(status=filters["status"])
    elif not batch:
        runs = runs.exclude(status__in=ACTIVE_RUNS)
    if not batch and filters.get("status") in ACTIVE_RUNS:
        active = active.exclude(pk__in=runs.values("pk"))
    active_runs = Paginator(active, 10).get_page(request.GET.get("active_page"))
    followups = case_runs.filter(previous_run=OuterRef("pk"))
    active_documents = (
        scoped_evidence(case, "document")
        .filter(is_archived=False)
        .annotate(document_key=Cast("pk", TextField()))
        .values("document_key")
    )
    ready_messages = (
        all_runs.filter(
            operation="imessage_import",
            method_version=OPERATIONS["imessage_import"].version,
            parameters__contains={"phase": "preview"},
            status__in=("complete", "partial"),
            result__case=case,
            result__data__metrics__ready_rows__gt=0,
        )
        .alias(document_key=KeyTextTransform.from_lookup("inputs__0__evidence_id"))
        .filter(~Exists(followups), document_key__in=active_documents)
    )
    showing_message_reviews = request.GET.get("reviews") == "messages" and not batch
    if showing_message_reviews:
        runs = ready_messages
    elif not batch and not filters:
        runs = runs.exclude(pk__in=ready_messages.values("pk"))
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    message_reviews = list(ready_messages[:6]) if not showing_message_reviews else []
    displayed_runs = [*active_runs, *page, *message_reviews[:5]]
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
                    ],
                    is_archived=False,
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
        if run.second_kind:
            run.source_title += " and other selected sources"
    all_batches = CaseAnalysisBatch.objects.filter(case=case).defer(
        "preparation_errors"
    )
    active_batches = Paginator(all_batches.filter(status="queued"), 5).get_page(
        request.GET.get("preparing_page")
    )
    batches = Paginator(all_batches.exclude(status="queued"), 10).get_page(
        request.GET.get("batches_page")
    )
    newer_batches = all_batches.filter(
        Q(created_at__gt=OuterRef("created_at"))
        | Q(created_at=OuterRef("created_at"), pk__lt=OuterRef("pk"))
    ).filter(
        Q(scope=OuterRef("scope"))
        | Q(
            Q(scope__type="reprocess"),
            scope__type=OuterRef("scope__type"),
            scope__kinds=OuterRef("scope__kinds"),
        )
        & (
            Q(scope__mode=OuterRef("scope__mode"))
            | Q(scope__analysis_version=OuterRef("scope__analysis_version"))
        )
        | Q(
            scope__type__in=("messages", "vehicle"),
            scope__type=OuterRef("scope__type"),
            scope__id=OuterRef("scope__id"),
        )
        | Q(
            Q(scope__type="instance_summaries"),
            scope__type=OuterRef("scope__type"),
            scope__people=OuterRef("scope__people"),
            scope__instances=OuterRef("scope__instances"),
        )
        | Q(scope__type="failed_retries", scope__type__exact=OuterRef("scope__type"))
    )
    failed_batches = list(
        all_batches.filter(status="failed")
        .filter(~Exists(newer_batches))
        .exclude(pk=batch.pk if batch else None)
        .only("scope", "created_at")[: NEEDS_ATTENTION_LIMIT + 1]
    )
    imports = case_imports(case).select_related("document")
    imports = imports.only(
        "id",
        "document__title",
        "document__is_archived",
        "status",
        "phase",
        "created_at",
        "processed_rows",
        "row_count",
    )
    imports_in_progress = imports.filter(status__in=ACTIVE_IMPORTS)
    active_imports = Paginator(imports_in_progress, 10).get_page(
        request.GET.get("active_imports_page")
    )
    actionable_import = Q(
        status__in=IMPORTS_AWAITING_ACTION, document__is_archived=False
    )
    imports_awaiting_action = imports.filter(actionable_import)
    import_reviews = list(imports_awaiting_action[:6])
    failed_imports = list(
        imports.filter(status="failed", document__is_archived=False)[
            : NEEDS_ATTENTION_LIMIT + 1
        ]
    )
    showing_import_reviews = request.GET.get("imports") == "review"
    import_history = Paginator(
        imports_awaiting_action
        if showing_import_reviews
        else imports.exclude(Q(status__in=ACTIVE_IMPORTS) | actionable_import),
        25,
    ).get_page(request.GET.get("imports_page"))
    call_imports = (
        case_call_imports(case)
        .select_related("document")
        .only(
            "document__title",
            "document__is_archived",
            "source_label",
            "created_at",
            "status",
            "is_running",
        )
    )
    active_call_imports = call_imports.filter(status__in=ACTIVE_CALL_IMPORTS)
    call_imports_to_review = call_imports.filter(
        status__in=(CallHistoryImport.Status.REVIEW, *FAILED_CALL_IMPORTS),
        document__is_archived=False,
    )
    call_import_counts = call_imports.aggregate(
        queued=Count("pk", filter=Q(status__in=ACTIVE_CALL_IMPORTS, is_running=False)),
        running=Count("pk", filter=Q(status__in=ACTIVE_CALL_IMPORTS, is_running=True)),
        complete=Count("pk", filter=Q(status=CallHistoryImport.Status.COMPLETED)),
        failed=Count("pk", filter=Q(status__in=FAILED_CALL_IMPORTS)),
    )
    call_import_history = Paginator(call_imports, 25).get_page(
        request.GET.get("call_imports_page")
    )
    trip_generations = (
        case_trip_generations(case)
        .select_related("vehicle")
        .defer("cursor", "carry", "metrics")
    )
    active_trips = Paginator(trip_generations.filter(ACTIVE_TRIPS), 10).get_page(
        request.GET.get("active_trips_page")
    )
    trip_history = Paginator(trip_generations.exclude(ACTIVE_TRIPS), 10).get_page(
        request.GET.get("trips_page")
    )
    failed_trips = list(
        trip_generations.filter(
            Q(status="failed") | Q(status="complete", geocode_status="failed"),
            pk__in=latest_generations(case).values("pk"),
        )[: NEEDS_ATTENTION_LIMIT + 1]
    )
    search_index = search_index_status(case)
    search_pending = search_index["status"] in ("queued", "running")
    context = case_context(case, request.user)
    run_counts, analysis_run_counts = _run_counts(case_runs)
    has_failed_runs = run_counts[AnalysisRun.Status.FAILED] > 0
    can_retry_failed = (
        has_failed_runs and context["can_edit"] and failed_checks(case).exists()
    )
    has_failed_checks = can_retry_failed or (
        has_failed_runs
        and case_runs.filter(status="failed").filter(~Exists(followups)).exists()
    )
    retry_submission = retry_after = None
    if can_retry_failed:
        latest_retry = latest_failed_retry(case)
        if (
            latest_retry is None
            or latest_retry.status != CaseAnalysisBatch.Status.QUEUED
        ):
            retry_submission = uuid4()
            retry_after = latest_retry.pk if latest_retry else None
    instagram_checks = (
        InstagramCheck.objects.filter(monitor__case=case)
        .select_related("monitor")
        .order_by("-started_at", "-pk")
    )
    monitor_counts = _status_counts(instagram_checks)
    running_monitors = monitor_counts.get("running", 0)
    monitor_history = Paginator(
        instagram_checks.exclude(status="running"), 10
    ).get_page(request.GET.get("instagram_page"))
    for check in monitor_history:
        check.message = check_message(check.error) if check.error else ""
    chat_turns = (
        ChatTurn.objects.filter(case=case, chat__case=case)
        .order_by("-created_at")
        .only("id", "chat_id", "question", "status", "total", "reviewed", "mode")
    )
    chat_counts = _status_counts(chat_turns)
    active_chat = chat_turns.filter(status__in=ACTIVE_RUNS)
    return render_live(
        request,
        "core/analysis/processing.html",
        context
        | {
            "nav_section": "processing",
            "processing_summary": _processing_summary(
                run_counts + Counter(chat_counts),
                all_batches,
                imports,
                call_import_counts,
                trip_generations,
                monitor_counts,
                search_index["status"],
            ),
            "analysis_run_counts": analysis_run_counts,
            "analysis_run_total": sum(row["count"] for row in analysis_run_counts),
            "page_obj": page,
            "active_runs": active_runs,
            "active_chat": active_chat[:10],
            "more_active_chat": sum(chat_counts.get(s, 0) for s in ACTIVE_RUNS) > 10,
            "failed_chat": chat_turns.filter(status="failed")[:NEEDS_ATTENTION_LIMIT],
            "message_reviews": message_reviews[:5],
            "more_message_reviews": len(message_reviews) > 5,
            "showing_message_reviews": showing_message_reviews,
            "import_reviews": import_reviews[:5],
            "more_import_reviews": len(import_reviews) > 5,
            "showing_import_reviews": showing_import_reviews,
            "failed_checks": has_failed_checks,
            "retry_submission": retry_submission,
            "retry_after": retry_after,
            "failed_batches": failed_batches[:NEEDS_ATTENTION_LIMIT],
            "more_failed_batches": len(failed_batches) > NEEDS_ATTENTION_LIMIT,
            "failed_imports": failed_imports[:NEEDS_ATTENTION_LIMIT],
            "more_failed_imports": len(failed_imports) > NEEDS_ATTENTION_LIMIT,
            "failed_trips": failed_trips[:NEEDS_ATTENTION_LIMIT],
            "more_failed_trips": len(failed_trips) > NEEDS_ATTENTION_LIMIT,
            "search_index": search_index,
            "search_pending": search_pending,
            "running_monitors": running_monitors,
            "monitor_history": monitor_history,
            "live_pending": bool(
                run_counts["queued"]
                or run_counts["running"]
                or any(chat_counts.get(s) for s in ACTIVE_RUNS)
                or active_batches.paginator.count
                or active_trips.paginator.count
                or search_pending
                or running_monitors
                or active_imports.paginator.count
                or call_import_counts["queued"]
                or call_import_counts["running"]
            ),
            "active_batches": active_batches,
            "active_imports": active_imports,
            "import_history": import_history,
            "active_call_imports": active_call_imports[:10],
            "more_active_call_imports": (
                call_import_counts["queued"] + call_import_counts["running"] > 10
            ),
            "call_imports_to_review": call_imports_to_review[:NEEDS_ATTENTION_LIMIT],
            "call_import_history": call_import_history,
            "active_trips": active_trips,
            "trip_history": trip_history,
            "batches": batches,
            "batch": batch,
            "batch_reader_url": batch_reader_url,
            "reprocessing_mode": MODE_LABELS.get(batch.scope.get("mode"))
            if batch and batch.scope.get("type") == "reprocess"
            else "",
            "search_needs_attention": context["can_edit"]
            and (
                search_index["status"] in {"changed", "failed"}
                or (
                    context["is_owner"]
                    and search_index["status"] == "awaiting_approval"
                )
            ),
            "batch_error_message": error_message(batch.error_code)
            if batch and batch.error_code
            else "",
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
            "show_history": bool(
                batch
                or showing_message_reviews
                or filters
                or request.GET.get("page")
                or request.GET.get("history") == "1"
            ),
            "history_suffix": query_suffix(
                ({"reviews": "messages"} if showing_message_reviews else filters)
                | ({"history_page": page.number} if page.number > 1 else {})
                | ({"batch": batch.pk} if batch else {})
                | (
                    {"return_evidence": request.GET["return_evidence"]}
                    if batch_reader_url
                    else {}
                )
                or {"history": "1"}
            ),
            "evidence_kinds": KIND_LABELS.items(),
            "statuses": AnalysisRun.Status.choices,
        },
    )

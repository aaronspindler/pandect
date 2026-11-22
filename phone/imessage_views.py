"""Retain one message export, review its people, and follow atomic publication."""

from uuid import UUID
from zoneinfo import ZoneInfo

from allauth.core import ratelimit
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Count, F, Q
from django.db.models.functions import TruncDate
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.import_checks import imported_sources
from core.analysis.navigation import history_query, processing_back_query, query_suffix
from core.analysis.operations import OPERATIONS
from core.evidence.queries import get_evidence, scoped_evidence
from core.models import AnalysisRun, CaseAnalysisBatch
from phone.analysis.deterministic.imessage import import_file_limit, read_import_page
from phone.conversations import conversation_url
from phone.imessage_forms import (
    IMessageConfirmForm,
    IMessagePersonForm,
    IMessageUploadForm,
)
from phone.imessage_imports import confirm_import, preview_source, start_preview
from phone.message_csv import IMPORT_FORMAT_LABELS
from ui.live import render_live

EARLIER_ANALYSIS_MODES = {
    "local": "Local checks",
    "ai": "AI only",
    "both": "Local checks and AI",
}


@product_login_required
@require_safe
def imessage_analysis_status(request, case_id, batch_id):
    case = get_case(request.user, case_id)
    batch = get_object_or_404(
        CaseAnalysisBatch,
        case=case,
        pk=batch_id,
        scope__type="messages",
        scope__automatic=True,
    )
    imported = get_object_or_404(
        AnalysisRun,
        case=case,
        pk=batch.scope["id"],
        operation="imessage_import",
        parameters__phase="import",
    )
    runs = (
        batch.runs.filter(case=case)
        .defer("parameters", "inputs")
        .annotate(execution=F("parameters___execution"))
    )
    counts = runs.aggregate(
        total=Count("pk"),
        pending=Count("pk", filter=Q(status__in=("queued", "running"))),
        completed=Count("pk", filter=Q(status__in=("complete", "empty"))),
        partial=Count("pk", filter=Q(status="partial")),
        insufficient=Count("pk", filter=Q(status="insufficient")),
        stopped=Count(
            "pk", filter=Q(status__in=("failed", "unsupported", "cancelled"))
        ),
    )
    live_pending = batch.status == "queued" or bool(counts["pending"])
    finished_count = counts["total"] - counts["pending"]
    first_message = None
    if not live_pending:
        try:
            _, published_messages = imported_sources(case, batch.scope)
        except AnalysisError, Http404:
            pass
        else:
            first_message = published_messages.order_by(
                "communication_context__source_row"
            ).first()
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    for run in page:
        operation = OPERATIONS.get(run.operation)
        run.label = operation.label if operation else "Retained analysis"
        run.failure_message = error_message(run.error_code) if run.error_code else ""
    return render_live(
        request,
        "phone/imessage_analysis_status.html",
        case_context(case, request.user)
        | {
            "nav_section": "processing",
            "batch": batch,
            "imported": imported,
            "analysis_mode": EARLIER_ANALYSIS_MODES.get(batch.scope.get("mode")),
            "page_obj": page,
            "finished_count": finished_count,
            "completed_count": counts["completed"],
            "partial_count": counts["partial"],
            "insufficient_count": counts["insufficient"],
            "stopped_count": counts["stopped"],
            "live_pending": live_pending,
            "conversation_url": conversation_url(
                case, first_message, open_details=False
            )
            if first_message
            else "",
            "preparation_error": error_message(batch.error_code)
            if batch.error_code
            else "",
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def imessage_upload(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    initial = {"timezone": request.user.timezone or "America/Toronto"}
    if request.method == "GET" and request.GET.get("document"):
        try:
            document_id = UUID(request.GET["document"])
        except ValueError:
            raise Http404("Document not found.") from None
        initial["document"] = get_evidence(case, "document", document_id).pk
    form = IMessageUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        case=case,
        initial=initial,
    )
    status = 200
    if request.method == "POST":
        status = 400
        if getattr(request, "evidence_upload_too_large", False):
            form.add_error(None, "Choose a CSV within the displayed import size limit.")
        elif sum(len(request.FILES.getlist(key)) for key in request.FILES) > 1:
            form.add_error(None, "Upload one CSV at a time.")
        elif form.is_valid():
            try:
                run, created = start_preview(request.user, case.pk, form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error.messages)
                status = 409
            except AnalysisError as error:
                form.add_error(None, error_message(error.code))
                status = 409
            except OSError, DatabaseError:
                form.add_error(
                    None,
                    "The CSV review could not be confirmed. "
                    "Select the same file and retry this submission.",
                )
                status = 503
            else:
                messages.success(
                    request,
                    "CSV review queued."
                    if created
                    else "This CSV review was already queued.",
                )
                return redirect("phone:imessage_review", case_id=case.pk, run_id=run.pk)
    return render(
        request,
        "phone/imessage_upload.html",
        case_context(case, request.user)
        | {
            "form": form,
            "nav_section": "evidence",
            "max_bytes": import_file_limit(),
        },
        status=status,
    )


def _first_reused_message(messages, case, document, run):
    people = run.parameters.get("people", {})
    account_id = people.get("account", {}).get("id")
    correspondent_id = people.get("correspondent", {}).get("id")
    if not account_id or not correspondent_id:
        return None
    zone = run.parameters["timezone"]
    messages = messages.filter(
        Q(sender_id=account_id, recipient_id=correspondent_id)
        | Q(sender_id=correspondent_id, recipient_id=account_id),
        communication_context__case=case,
        communication_context__source_document=document,
        communication_context__source_sha256=document.sha256,
        communication_context__timezone=zone,
    )
    date_filters = {
        f"export_date__{lookup}": run.parameters[field]
        for field, lookup in (("start_date", "gte"), ("end_date", "lte"))
        if run.parameters.get(field)
    }
    if date_filters:
        messages = messages.alias(
            export_date=TruncDate("occurred_at", tzinfo=ZoneInfo(zone))
        ).filter(**date_filters)
    return messages.order_by("communication_context__source_row").first()


@product_login_required
@require_http_methods(["GET", "POST"])
def imessage_review(request, case_id, run_id):
    case = get_case(request.user, case_id, edit=request.method == "POST")
    requested = get_object_or_404(
        AnalysisRun.objects.select_related("result", "previous_run").filter(
            case=case, operation="imessage_import"
        ),
        pk=run_id,
    )
    if requested.parameters.get("phase") == "import":
        preview = get_object_or_404(
            AnalysisRun.objects.select_related("result"),
            pk=requested.parameters.get("preview_run_id"),
            case=case,
            operation="imessage_import",
            parameters__phase="preview",
        )
    else:
        preview = requested
    final = (
        AnalysisRun.objects.filter(
            case=case,
            operation="imessage_import",
            parameters__phase="import",
            parameters__preview_run_id=str(preview.pk),
        )
        .select_related("result")
        .first()
    )
    current = final or preview
    review_query = history_query(request.GET)
    back_query = processing_back_query(review_query, batch_id=requested.batch_id)
    if "batch" not in back_query:
        review_query.pop("batch", None)
    review_navigation = {
        "processing_back_url": reverse(
            "core:analysis:processing",
            args=(case.pk,),
            query=back_query,
            fragment="processing-history",
        )
        if back_query
        else "",
        "review_suffix": query_suffix(review_query),
    }
    if (
        request.method in {"GET", "HEAD"}
        and request.headers.get("X-Pandect-Live") == "1"
    ):
        # Poll only authorized run state. Reading rows deliberately revalidates
        # the entire CSV, so it must never run on an automatic refresh.
        return render_live(
            request,
            "phone/imessage_progress.html",
            {"case": case, "run": current, "final": final} | review_navigation,
        )
    excluded_only = request.GET.get("rows") == "excluded"
    show_rows = excluded_only or request.GET.get("page") is not None
    if show_rows or request.method == "POST":
        limited = ratelimit.consume_or_429(
            request,
            action="imessage_row_review",
            key=str(case.pk),
            limit_get=True,
        )
        if limited is not None:
            return limited
    document = None
    source_error = ""
    try:
        source = preview_source(preview, case)
        document = source.item
    except AnalysisError as error:
        source_error = error_message(error.code)
    if any(
        hasattr(run, "result") and run.result.case_id != case.pk
        for run in (preview, current)
    ):
        source_error = error_message("source_unavailable")
    data = (
        preview.result.data if not source_error and hasattr(preview, "result") else {}
    )
    legacy_preview = preview.method_version != OPERATIONS["imessage_import"].version
    samples = data.get("imessage_samples", data.get("items", [])[:3])
    metrics = data.get("metrics", {})
    excluded_count = metrics.get("unresolved_rows", 0) + metrics.get(
        "date_excluded_rows", 0
    )
    rows = Paginator(data.get("items", []), 25).get_page(request.GET.get("page"))
    if data and not legacy_preview and show_rows and request.method != "POST":
        try:
            total = excluded_count if excluded_only else metrics.get("source_rows", 0)
            rows = Paginator(range(total), 25).get_page(request.GET.get("page"))
            selected, actual_total, samples = read_import_page(
                source,
                preview.parameters
                | {
                    "_imessage_text_limit_bytes": data["imessage"]["text_limit_bytes"],
                    "_imessage_interpretation_sha256": data["imessage"][
                        "interpretation_sha256"
                    ],
                },
                page=rows.number,
                per_page=25,
                excluded_only=excluded_only,
            )
            if actual_total != total:
                raise AnalysisError("imessage_source_changed")
            rows.object_list = selected
        except AnalysisError as error:
            source_error = error_message(error.code)
            data, samples = {}, []
            rows = Paginator([], 25).get_page(1)
    metrics = data.get("metrics", {})
    ready = bool(
        not source_error
        and not legacy_preview
        and document
        and preview.status in ("complete", "partial")
        and metrics.get("ready_rows", 0) > 0
    )
    form = account = correspondent = None
    status = 200
    if ready:
        posted = request.POST if request.method == "POST" else None
        form = IMessageConfirmForm(posted)
        account = IMessagePersonForm(
            posted, case=case, preview=preview, role="account", prefix="account"
        )
        correspondent = IMessagePersonForm(
            posted,
            case=case,
            preview=preview,
            role="correspondent",
            prefix="correspondent",
        )
        if request.method == "POST":
            valid = all([form.is_valid(), account.is_valid(), correspondent.is_valid()])
            if valid:
                try:
                    final, created = confirm_import(
                        request.user,
                        case.pk,
                        preview.pk,
                        {
                            "account": account.specification(),
                            "correspondent": correspondent.specification(),
                        },
                    )
                except ValidationError as error:
                    form.add_error(None, error.messages)
                    status = 409
                except AnalysisError as error:
                    form.add_error(None, error_message(error.code))
                    status = 409
                except DatabaseError:
                    form.add_error(
                        None,
                        "The import request could not be confirmed. "
                        "Retry these same selections.",
                    )
                    status = 503
                else:
                    messages.success(
                        request,
                        "Message import queued."
                        if created
                        else "This message import was already queued.",
                    )
                    return redirect(
                        reverse(
                            "phone:imessage_review",
                            args=(case.pk, final.pk),
                            query=review_query,
                        )
                    )
            else:
                status = 400
    elif request.method == "POST":
        status = 409
        source_error = (
            source_error
            or (
                "Prepare another review from the retained CSV before importing "
                "this earlier format."
                if legacy_preview
                else ""
            )
            or "There are no ready messages to import. "
            "Review the source and prepare another CSV review."
        )
    result_data = (
        current.result.data if not source_error and hasattr(current, "result") else {}
    )
    retained_messages = scoped_evidence(case, "text-message")
    imported = retained_messages.none()
    first_message = None
    if result_data.get("import_publication_version") == 2:
        imported = retained_messages.filter(
            communication_context__case=case,
            communication_context__source_document=document,
            communication_context__source_sha256=document.sha256,
            communication_context__import_run=current,
        ).order_by("communication_context__source_row")
        first_message = imported.first()
        if first_message is None and result_data.get("metrics", {}).get("reused_rows"):
            first_message = _first_reused_message(
                retained_messages, case, document, current
            )
    elif result_data:
        message_ids = [
            row.get("values", {}).get("message_id")
            for row in result_data.get("items", [])
            if row.get("values", {}).get("message_id")
        ]
        imported = retained_messages.filter(pk__in=message_ids)
        first_message = imported.first()
    imported = Paginator(imported, 25).get_page(request.GET.get("messages_page"))
    summary = data.get("imessage", {})
    return render_live(
        request,
        "phone/imessage_review.html",
        case_context(case, request.user)
        | review_navigation
        | {
            "nav_section": "evidence",
            "preview": preview,
            "run": current,
            "document": document,
            "source_error": source_error,
            "summary": summary,
            "format_label": IMPORT_FORMAT_LABELS.get(summary.get("parser_version")),
            "samples": samples,
            "metrics": metrics,
            "rows": rows,
            "show_rows": show_rows,
            "excluded_only": excluded_only,
            "excluded_count": excluded_count if metrics else 0,
            "processing_details_url": reverse(
                "core:analysis:detail",
                args=(case.pk, current.pk),
                query=review_query | {"processing": "1"},
            ),
            "form": form,
            "account_form": account,
            "correspondent_form": correspondent,
            "can_confirm": ready and final is None,
            "final": final,
            "published": final is not None
            and current.status in ("complete", "partial"),
            "legacy_preview": legacy_preview,
            "imported": imported,
            "conversation_url": conversation_url(
                case, first_message, open_details=False
            )
            if first_message
            else "",
            "publication_metrics": result_data.get("metrics", {}),
            "publication_version": result_data.get("import_publication_version"),
            "processing_error": error_message(current.error_code)
            if current.error_code
            else "",
        },
        status=status,
    )

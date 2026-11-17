"""Retain one message export, review its people, and follow atomic publication."""

from uuid import UUID

from allauth.core import ratelimit
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.services import resolve_inputs
from core.evidence.queries import get_evidence, scoped_evidence
from core.models import AnalysisRun
from phone.analysis.deterministic.imessage import import_file_limit, read_import_page
from phone.imessage_forms import (
    IMessageConfirmForm,
    IMessagePersonForm,
    IMessageUploadForm,
)
from phone.imessage_imports import confirm_import, preview_source, start_preview
from ui.live import render_live


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
    if (
        request.method in {"GET", "HEAD"}
        and request.headers.get("X-Pandect-Live") == "1"
    ):
        # Poll only authorized run state. Reading rows deliberately revalidates
        # the entire CSV, so it must never run on an automatic refresh.
        return render_live(
            request,
            "phone/imessage_progress.html",
            {"case": case, "run": current, "final": final},
        )
    show_rows = request.GET.get("page") is not None
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
        document = preview_source(preview, case)
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
    legacy_preview = preview.method_version != "2"
    samples = data.get("imessage_samples", data.get("items", [])[:3])
    rows = Paginator(data.get("items", []), 25).get_page(request.GET.get("page"))
    if data and not legacy_preview and (show_rows or request.method == "POST"):
        try:
            total = data.get("metrics", {}).get("source_rows", 0)
            rows = Paginator(range(total), 25).get_page(request.GET.get("page"))
            source = resolve_inputs(preview, case)[0]
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
        form = IMessageConfirmForm(posted, actor=request.user, case=case, run=preview)
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
                        analysis=form.specification(),
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
                        "phone:imessage_review", case_id=case.pk, run_id=final.pk
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
    imported = scoped_evidence(case, "text-message").none()
    if result_data.get("import_publication_version") == 2:
        imported = (
            scoped_evidence(case, "text-message")
            .filter(
                communication_context__case=case,
                communication_context__source_document=document,
                communication_context__source_sha256=document.sha256,
                communication_context__import_run=current,
            )
            .order_by("communication_context__source_row")
        )
    elif result_data:
        message_ids = [
            row.get("values", {}).get("message_id")
            for row in result_data.get("items", [])
            if row.get("values", {}).get("message_id")
        ]
        imported = scoped_evidence(case, "text-message").filter(pk__in=message_ids)
    imported = Paginator(imported, 25).get_page(request.GET.get("messages_page"))
    return render_live(
        request,
        "phone/imessage_review.html",
        case_context(case, request.user)
        | {
            "nav_section": "evidence",
            "preview": preview,
            "run": current,
            "document": document,
            "source_error": source_error,
            "summary": data.get("imessage", {}),
            "samples": samples,
            "metrics": metrics,
            "rows": rows,
            "show_rows": show_rows,
            "form": form,
            "account_form": account,
            "correspondent_form": correspondent,
            "can_confirm": ready and final is None,
            "final": final,
            "legacy_preview": legacy_preview,
            "imported": imported,
            "publication_metrics": result_data.get("metrics", {}),
            "publication_version": result_data.get("import_publication_version"),
            "processing_error": error_message(current.error_code)
            if current.error_code
            else "",
        },
        status=status,
    )

"""Prepare, review, and publish call history from a retained case CSV."""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, F, Min, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.evidence.navigation import query_suffix
from core.evidence.queries import get_evidence
from core.evidence.responses import detail_url
from core.evidence.types import CALL_KINDS
from phone.call_history import MAX_ROWS, import_file_limit
from phone.call_history_jobs import (
    ERROR_MESSAGES,
    MAX_ATTEMPTS,
    confirm_call_history,
    retain_call_history,
    retry_call_history,
    save_call_history_selection,
)
from phone.call_import_forms import (
    CallHistorySelectionForm,
    CallHistoryUploadForm,
    LinkCallPersonForm,
)
from phone.models import CallHistoryImport, CallHistoryRow
from phone.models import link_imported_person as save_person_link
from ui.live import render_live

CALL_HISTORY_REVIEW = "phone:call_history_review"
ROWS_PER_PAGE = 25
EARLIER_ROWS_PER_PAGE = 5
REVIEW_TABS = {
    "people": "Person matches",
    "rows": "Source rows",
    "overlaps": "Possible overlaps",
    "held": "Rows held for review",
}


@product_login_required
@require_http_methods(["GET", "POST"])
def call_history_upload(request, case_id):
    case = get_case(request.user, case_id, edit=True)
    is_post = request.method == "POST"
    form = CallHistoryUploadForm(
        request.POST if is_post else None,
        request.FILES if is_post else None,
        case=case,
        byte_limit=import_file_limit(),
    )
    source_document = None
    if value := request.GET.get("document"):
        try:
            source_document = form.fields["document"].queryset.filter(pk=value).first()
        except ValidationError:
            pass
        if source_document:
            form.initial["document"] = source_document.pk
    status = 200
    if is_post:
        status = 400
        if getattr(request, "evidence_upload_too_large", False):
            form.add_error(None, "Choose a CSV within the displayed size limit.")
            status = 413
        elif sum(len(files) for _, files in request.FILES.lists()) > 1:
            form.add_error(None, "Choose one CSV at a time.")
        elif form.is_valid():
            data = form.cleaned_data
            try:
                batch = retain_call_history(
                    request.user,
                    case.pk,
                    submission_id=data["submission_id"],
                    original=data["file"],
                    document=data["document"],
                    source=data["source"],
                    source_label=data["source_label"],
                    timezone=data["timezone"],
                )
            except ValidationError as error:
                form.add_error(None, error)
            except OSError:
                form.add_error(
                    None,
                    "The CSV could not be retained. Keep your file and try again.",
                )
                status = 503
            else:
                return redirect(CALL_HISTORY_REVIEW, case.pk, batch.pk)
    return render(
        request,
        "phone/call_history_upload.html",
        {
            **case_context(case, request.user),
            "form": form,
            "source_document": source_document,
            "max_csv_bytes": import_file_limit(),
            "max_csv_rows": f"{MAX_ROWS:,}",
            "nav_section": "evidence",
        },
        status=status,
    )


def _review_url(request, batch, view, page):
    base = reverse(CALL_HISTORY_REVIEW, args=[batch.case_id, batch.pk])
    parameters = {"view": view, "page": page}
    if view == "rows":
        for field in ("row", "group", "earlier_page"):
            if value := _row_number(request.GET.get(field, "")):
                parameters[field] = value
    return f"{base}?{urlencode(parameters)}#call-review-choices"


def _row_number(value):
    if value.isascii() and value.isdecimal() and len(value) <= 9:
        return int(value)
    return None


def _review_rows(request, rows, view):
    if view == "people":
        source = (
            rows.filter(issue="")
            .exclude(identifier="")
            .values("identifier")
            .annotate(call_count=Count("pk"), group_row=Min("number"))
            .order_by("identifier")
        )
    elif view == "held":
        source = rows.exclude(issue="")
    elif view == "overlaps":
        source = rows.exclude(overlap_candidates=[])
    else:
        source = rows
        if group := _row_number(request.GET.get("group", "")):
            identifier = (
                rows.filter(number=group, issue="")
                .values_list("identifier", flat=True)
                .first()
            )
            source = (
                source.filter(identifier=identifier, issue="")
                if identifier
                else source.none()
            )
        if number := _row_number(request.GET.get("row", "")):
            source = source.filter(number=number)
    return Paginator(source, ROWS_PER_PAGE).get_page(request.GET.get("page"))


def _row_details(request, rows, batch, case, form):
    focus_row = len(rows) == 1 and request.GET.get("row") == str(rows[0].number)
    earlier_pages = {
        row.number: Paginator(row.overlap_candidates, EARLIER_ROWS_PER_PAGE).get_page(
            request.GET.get("earlier_page") if focus_row else 1
        )
        for row in rows
        if row.overlap_candidates
    }
    candidate_ids = {
        key for earlier_page in earlier_pages.values() for key in earlier_page
    }
    candidates = (
        CallHistoryRow.objects.filter(
            pk__in=candidate_ids,
            batch__case=case,
            batch__source=batch.source,
            batch__document__case=case,
            batch__source_sha256=F("batch__document__sha256"),
            batch__status=CallHistoryImport.Status.COMPLETED,
            disposition=CallHistoryRow.Disposition.PUBLISHED,
        )
        .exclude(batch=batch)
        .select_related("batch__document")
        .only("number", "batch_id", "batch__document__original_filename")
    )
    by_id = {str(candidate.pk): candidate for candidate in candidates}
    fingerprints = {row.fingerprint for row in rows if row.overlap_candidates}
    incoming_counts = {
        value["fingerprint"]: value["count"]
        for value in batch.rows.filter(fingerprint__in=fingerprints, issue="")
        .order_by()
        .values("fingerprint")
        .annotate(count=Count("pk"))
    }
    return [
        {
            "row": row,
            "candidates": [
                by_id[key] for key in earlier_pages.get(row.number, ()) if key in by_id
            ],
            "earlier_page": earlier_pages.get(row.number),
            "focus_row": focus_row,
            "incoming_count": incoming_counts.get(row.fingerprint, 0),
            "person_field": form[f"person_{row.number}"]
            if form and f"person_{row.number}" in form.fields
            else None,
            "overlap_field": form[f"overlap_{row.number}"]
            if form and f"overlap_{row.number}" in form.fields
            else None,
        }
        for row in rows
    ]


@product_login_required
@require_http_methods(["GET", "POST"])
def call_history_review(request, case_id, import_id):
    is_post = request.method == "POST"
    case = get_case(request.user, case_id, edit=is_post)
    batch = get_object_or_404(
        CallHistoryImport.objects.filter(
            case=case,
            document__case=case,
            source__case=case,
            source_sha256=F("document__sha256"),
        ).select_related("document", "source"),
        pk=import_id,
    )
    context = case_context(case, request.user)
    status = 200
    status_values = CallHistoryImport.Status
    processing = batch.status in {status_values.PREPARING, status_values.PUBLISHING}
    failed = batch.status in {
        status_values.PREPARE_FAILED,
        status_values.PUBLISH_FAILED,
    }
    completed = batch.status == status_values.COMPLETED
    review_ready = (
        batch.status == status_values.REVIEW and not batch.document.is_archived
    )
    view = request.GET.get("view", "rows" if completed else "people")
    if view not in REVIEW_TABS or (completed and view == "people"):
        view = "rows"
    rows = batch.rows.order_by("number")
    counts = rows.aggregate(
        total=Count("pk"),
        held=Count("pk", filter=~Q(issue="")),
        overlaps=Count("pk", filter=~Q(overlap_candidates=[]) & Q(issue="")),
        published=Count("pk", filter=Q(disposition="published")),
        skipped=Count("pk", filter=Q(disposition="skipped")),
    )
    counts["ready"] = counts["total"] - counts["held"]
    overlaps_need_confirmation = (
        counts["overlaps"] > 0 and "overlap_keep" not in batch.selection
    )
    page_rows_query = (
        rows.select_related("phonecallevidence", "facetimeevidence")
        if completed
        else rows
    )
    page_obj = (
        _review_rows(request, page_rows_query, view)
        if review_ready or completed
        else None
    )
    page_number = page_obj.number if page_obj else 1
    groups = list(page_obj) if page_obj and view == "people" else []
    page_rows = list(page_obj) if page_obj and view != "people" else []
    form = None
    if review_ready and context["can_edit"]:
        form = CallHistorySelectionForm(
            request.POST if is_post else None,
            case=case,
            batch=batch,
            view=view,
            groups=groups,
            rows=page_rows,
        )
    error_message = ""
    if is_post:
        status = 400
        action = request.POST.get("action")
        try:
            if batch.document.is_archived:
                raise ValidationError("Restore this CSV before continuing the import.")
            if completed or (
                batch.status == status_values.PUBLISHING and action == "publish"
            ):
                return redirect(CALL_HISTORY_REVIEW, case.pk, batch.pk)
            if action == "retry":
                retry_call_history(
                    request.user,
                    case.pk,
                    batch.pk,
                    worker_stopped=request.POST.get("worker_stopped") == "on",
                )
                return redirect(CALL_HISTORY_REVIEW, case.pk, batch.pk)
            if not form or action not in {"save", "publish"}:
                raise ValidationError(
                    "Refresh this import to see the available actions."
                )
            if form.is_valid():
                reviewed_attempt = form.cleaned_data["reviewed_attempt"]
                if action == "publish" and not counts["ready"]:
                    raise ValidationError("This CSV has no calls ready to publish.")
                if (
                    action == "publish"
                    and overlaps_need_confirmation
                    and view != "overlaps"
                ):
                    raise ValidationError(
                        "Choose how to handle possible overlaps before publishing."
                    )
                saved = save_call_history_selection(
                    request.user,
                    case.pk,
                    batch.pk,
                    reviewed_attempt=reviewed_attempt,
                    **form.choices(),
                )
                if action == "publish":
                    confirm_call_history(
                        request.user,
                        case.pk,
                        batch.pk,
                        reviewed_attempt=saved.attempt_id,
                    )
                    return redirect(CALL_HISTORY_REVIEW, case.pk, batch.pk)
                messages.success(request, "Review choices saved.")
                return redirect(_review_url(request, batch, view, page_number))
        except ValidationError as error:
            error_message = " ".join(error.messages)
    return render_live(
        request,
        "phone/call_history_review.html",
        {
            **context,
            "batch": batch,
            "view": view,
            "tabs": REVIEW_TABS,
            "form": form,
            "page_obj": page_obj,
            "groups": [
                (group, form[f"group_{index}"] if form else None)
                for index, group in enumerate(groups)
            ],
            "rows": _row_details(request, page_rows, batch, case, form)
            if page_rows
            else (),
            "counts": counts,
            "overlaps_need_confirmation": overlaps_need_confirmation,
            "can_publish": counts["ready"]
            and (not overlaps_need_confirmation or view == "overlaps"),
            "review_ready": review_ready,
            "processing": processing,
            "can_retry": (
                context["can_edit"]
                and not batch.document.is_archived
                and batch.attempts < MAX_ATTEMPTS
            ),
            "failed": failed,
            "completed": completed,
            "error_message": error_message,
            "processing_error": ERROR_MESSAGES.get(batch.error),
            "nav_section": "evidence",
        },
        status=status,
    )


@product_login_required
@require_http_methods(["GET", "POST"])
def link_imported_person(request, case_id, kind, evidence_id):
    if kind not in CALL_KINDS:
        raise Http404("Call not found.")
    case = get_case(request.user, case_id, edit=True)
    item = get_evidence(case, kind, evidence_id)
    if not item.import_row_id:
        raise Http404("Imported call not found.")
    is_post = request.method == "POST"
    reader_url = detail_url(case, kind, item) + query_suffix(request)
    form = LinkCallPersonForm(
        request.POST if is_post else None,
        case=case,
        item=item,
    )
    status = 200
    if item.is_archived or not form.fields["role"].choices:
        status = 409
        form = None
    elif is_post:
        status = 400
        if form.is_valid():
            try:
                save_person_link(item, actor=request.user, **form.cleaned_data)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, "Person linked to the call.")
                return redirect(reader_url)
    return render(
        request,
        "phone/link_call_person.html",
        {
            **case_context(case, request.user),
            "item": item,
            "form": form,
            "reader_url": reader_url,
            "nav_section": "evidence",
        },
        status=status,
    )

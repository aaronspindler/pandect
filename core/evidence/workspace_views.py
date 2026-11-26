"""Read the case evidence collection, originals, and their saved findings."""

from django.http import Http404, HttpResponse
from django.urls import reverse
from django.views.decorators.http import (
    require_safe,
)
from django.views.decorators.vary import vary_on_headers

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError
from core.analysis.findings import evidence_findings_context
from core.evidence.collection import collection_context
from core.evidence.form_registry import TYPE_FIELDS
from core.evidence.navigation import list_params, query_suffix
from core.evidence.queries import evidence_counts_by_type, get_evidence
from core.evidence.responses import (
    file_download,
)
from core.evidence.types import (
    CALL_KINDS,
    COMMUNICATION_KINDS,
    KIND_LABELS,
    LOCATION_KINDS,
    READER_TEMPLATES,
)
from core.important.reader import reader_context as important_reader_context
from geo.models import VehicleLocationImport
from image.previews import image_preview
from ui.dialogs import render_record, wants_record
from ui.live import render_live

EVIDENCE_SUMMARY_ICONS = {
    "image": "photo",
    "audio": "music",
    "document": "file-text",
    "text": "file-text",
    "location": "map",
    "person-location": "users",
    "vehicle-location": "car",
    "text-message": "message",
    "phone-call": "phone",
    "facetime": "phone",
}


def _list_context(request, case, *, workspace_rows=True):
    context = collection_context(
        case, request.GET, actor=request.user, workspace_rows=workspace_rows
    )
    if workspace_rows:
        for row in context["rows"]:
            row["icon"] = EVIDENCE_SUMMARY_ICONS.get(row["kind"], "file-text")
    return context


@product_login_required
@require_safe
def evidence_list(request, case_id):
    case = get_case(request.user, case_id)
    gallery = (
        request.GET.get("type") == "image" and request.GET.get("view") == "gallery"
    )
    context = case_context(case, request.user) | _list_context(
        request, case, workspace_rows=not gallery
    )
    context["nav_section"] = "evidence"
    if gallery:
        return render_live(request, "image/gallery.html", context, collection=True)
    state = context["state"]
    counts = evidence_counts_by_type(
        case, archived=None if state == "all" else state == "archived"
    )
    context["evidence_summary_heading"] = {
        "active": "Active evidence by type",
        "archived": "Archived evidence by type",
        "all": "All evidence by type",
    }[state]
    context["evidence_summary"] = [
        {
            "label": label,
            "count": counts[kind],
            "icon": EVIDENCE_SUMMARY_ICONS.get(kind),
        }
        for kind, label in KIND_LABELS.items()
    ]
    return render_live(
        request, "core/evidence/workspace.html", context, collection=True
    )


@product_login_required
@require_safe
@vary_on_headers("X-Pandect-Form")
def detail(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    item = get_evidence(
        case,
        kind,
        evidence_id,
        identity_only=request.headers.get("X-Pandect-Live") in {"1", "collection"},
    )
    return render_detail(request, case, kind, item)


def render_detail(request, case, kind, item, *, analysis_form=None, status=200):
    """Render the complete reader for an already-authorized case and original."""
    context = case_context(case, request.user) | {"item": item, "kind": kind}
    mode = (
        request.headers.get("X-Pandect-Live")
        if request.method in {"GET", "HEAD"}
        else None
    )
    record_only = wants_record(request) and mode is None
    if mode != "1" and not record_only:
        context.update(_list_context(request, case))
    if mode == "collection":
        return render_live(
            request, "core/evidence/workspace.html", context, collection=True
        )
    if not record_only:
        context.update(
            evidence_findings_context(case, kind, item, request.GET, form=analysis_form)
        )
    if mode == "1":
        context["list_suffix"] = query_suffix(request)
        return render_live(request, "core/evidence/workspace.html", context)
    technical = [
        (item._meta.get_field(name).verbose_name.capitalize(), getattr(item, name))
        for name in TYPE_FIELDS[kind]
        if getattr(item, name) not in (None, "")
    ]
    context.update(
        {
            "kind_label": KIND_LABELS[kind],
            "reader_template": READER_TEMPLATES[kind],
            "technical_details": technical,
            "nav_section": "evidence",
            "is_location": kind in LOCATION_KINDS,
            "is_communication": kind in COMMUNICATION_KINDS,
            "is_call": kind in CALL_KINDS,
            "can_delete_evidence": request.user.is_superuser,
            "info_open": request.GET.get("info") == "1"
            or request.GET.get("section") == "details"
            or any(key in request.GET for key in ("thread_page", "reply_page")),
        }
    )
    if record_only:
        if kind == "text-message":
            from phone.presentation import message_evidence_label

            context["display_title"] = message_evidence_label(
                item.body, item.sender, item.occurred_at
            )
        return render_record(request, "core/evidence/workspace.html", context)
    reader_params = list_params(request)
    for key in (
        "findings_page",
        "results_page",
        "thread_page",
        "reply_page",
        "conversation_list_page",
    ):
        page = request.GET.get(key, "")
        if len(page) <= 9 and page.isascii() and page.isdecimal():
            reader_params[key] = page
    if context["info_open"]:
        reader_params["info"] = "1"
    context.update(
        important_reader_context(
            request,
            case,
            evidence_kind=kind,
            evidence_id=item.pk,
            source_url=reverse(
                "core:evidence:detail",
                args=[case.pk, kind, item.pk],
                query=reader_params,
                fragment="evidence-reader",
            ),
        )
    )
    if kind in COMMUNICATION_KINDS:
        from phone.conversations import conversation_list_page
        from phone.presentation import reader_context

        context.update(
            reader_context(
                case,
                kind,
                item,
                conversation_list_page=conversation_list_page(
                    request.GET.get("conversation_list_page")
                ),
                native_thread_page=request.GET.get("thread_page"),
                native_reply_page=request.GET.get("reply_page"),
            )
        )
    if kind == "document":
        from phone.models import CallHistoryImport

        context["vehicle_import"] = VehicleLocationImport.objects.filter(
            case=case, document=item
        ).first()
        context["call_history_csv"] = item.original_filename.lower().endswith(".csv")
        if context["call_history_csv"]:
            context["call_history_import"] = (
                CallHistoryImport.objects.filter(
                    case=case,
                    document=item,
                    source__case=case,
                    source_sha256=item.sha256,
                )
                .order_by("-created_at", "-pk")
                .first()
            )
    elif kind == "vehicle-location":
        from geo.trip_processing import observation_trip_url

        context["observation_trip_url"] = observation_trip_url(case, item)
    return render_live(request, "core/evidence/workspace.html", context, status=status)


@product_login_required
@require_safe
def preview(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    if kind != "image":
        raise Http404
    item = get_evidence(case, kind, evidence_id)
    try:
        content = image_preview(item, thumbnail=request.GET.get("size") == "thumbnail")
    except AnalysisError, OSError, ValueError:
        response = HttpResponse(
            "Image preview unavailable. Download the original to inspect it.",
            status=422,
            content_type="text/plain",
        )
    else:
        response = HttpResponse(content, content_type="image/jpeg")
        response["Content-Disposition"] = f'inline; filename="preview-{item.pk}.jpg"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@product_login_required
@require_safe
def download(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id)
    item = get_evidence(case, kind, evidence_id)
    # Linux WebKit can render typed attachments inline; use a download MIME type.
    if kind == "text":
        response = HttpResponse(
            item.body.encode("utf-8"), content_type="application/octet-stream"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="evidence-{item.pk}.txt"'
        )
    elif kind in (*LOCATION_KINDS, *COMMUNICATION_KINDS):
        response = HttpResponse(
            item.original_bytes(), content_type="application/octet-stream"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="evidence-{item.pk}.json"'
        )
    else:
        response = file_download(request, case, kind, item)
    response["X-Content-Type-Options"] = "nosniff"
    return response

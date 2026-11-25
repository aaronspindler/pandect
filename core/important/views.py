from collections import defaultdict
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.catalogue import archived_finding_sources, raw_finding_item
from core.analysis.important import finding_target
from core.analysis.services import SourceResolutionCache
from core.evidence.queries import scoped_evidence
from core.evidence.types import KIND_LABELS
from core.models import ImportantItem, ImportantNote
from phone.presentation import message_labels
from ui.dialogs import form_saved, render_quick_form
from ui.live import render_live

from .navigation import list_url, page_number, with_return
from .reader import reader_return_token, reader_return_url
from .services import (
    RESULT_RELATION,
    InvalidMarkAction,
    evidence_target,
    read_note_item,
    reply_target,
    set_evidence_mark,
    set_finding_mark,
    set_reply_mark,
    submit_note,
)

PER_PAGE = 25


class ImportantNoteForm(forms.Form):
    body = forms.CharField(
        label="Note", max_length=4000, widget=forms.Textarea(attrs={"rows": 4})
    )


def _page_rows(case, page):
    evidence_ids = defaultdict(list)
    for item in page:
        if item.evidence_kind in KIND_LABELS:
            evidence_ids[item.evidence_kind].append(item.evidence_id)
    evidence = {
        (kind, record.pk): record
        for kind, ids in evidence_ids.items()
        for record in scoped_evidence(case, kind)
        .filter(pk__in=ids, is_archived=False)
        .only("title")
    }
    message_titles = message_labels(case, evidence_ids.get("text-message", []))
    counts = dict(
        ImportantNote.objects.filter(item__in=page.object_list)
        .values_list("item_id")
        .annotate(total=Count("pk"))
    )
    rows = []
    cached_result_id = None
    source_cache = None
    for item in page:
        try:
            if item.chat_turn_id:
                target = reply_target(case, item.chat_turn)
            elif item.finding_result_id:
                if item.finding_result_id != cached_result_id:
                    # Avoid keeping full source text from several results in memory.
                    cached_result_id = item.finding_result_id
                    source_cache = SourceResolutionCache(case, active_only=True)
                target = finding_target(
                    case,
                    item.finding_result,
                    item.item_index,
                    finding=item.raw_item,
                    cache=source_cache,
                )
            else:
                original = evidence[(item.evidence_kind, item.evidence_id)]
                target = evidence_target(
                    case,
                    item.evidence_kind,
                    original,
                    title=message_titles.get(original.pk)
                    if item.evidence_kind == "text-message"
                    else None,
                )
        except Http404, KeyError:
            rows.append({"item": item})
            continue
        params = {"important_page": page.number, "important_item": str(item.pk)}
        rows.append(
            {
                "item": item,
                "target": target,
                "source_url": with_return(target["url"], params),
                "notes_url": reverse(
                    "core:important:notes",
                    args=[case.pk, item.pk],
                    query={"important_page": page.number},
                ),
                "note_count": counts.get(item.pk, 0),
            }
        )
    return rows


@product_login_required
@require_safe
def index(request, case_id):
    case = get_case(request.user, case_id)
    archived = Q(pk__in=[])
    for kind in KIND_LABELS:
        archived |= Q(
            evidence_kind=kind,
            evidence_id__in=scoped_evidence(case, kind)
            .filter(is_archived=True)
            .values("pk"),
        )
    items = (
        ImportantItem.objects.filter(case=case, marked_at__isnull=False)
        .exclude(archived)
        .filter(
            ~Exists(
                archived_finding_sources(case).filter(
                    finding__result_id=OuterRef("finding_result_id"),
                )
            )
        )
        .select_related("marked_by", "chat_turn__chat", RESULT_RELATION)
        .defer(
            "finding_result__data",
            "finding_result__provenance",
            "finding_result__run__previous_run__inputs",
            "finding_result__run__previous_run__parameters",
        )
        .annotate(raw_item=raw_finding_item("finding_result__data"))
        .order_by("-marked_at", "-pk")
    )
    page = Paginator(items, PER_PAGE).get_page(page_number(request.GET.get("page", "")))
    return render_live(
        request,
        "core/important/list.html",
        case_context(case, request.user)
        | {"rows": _page_rows(case, page), "page_obj": page},
        collection=True,
    )


def _mark_response(request, change_mark, *target, evidence=False):
    action = request.POST.get("action")
    try:
        source_url = change_mark(request.user, *target, action=action)
    except InvalidMarkAction:
        return HttpResponseBadRequest("Choose whether to mark or remove this item.")
    messages.success(
        request, "Marked important." if action == "mark" else "Removed from Important."
    )
    reader_url = reader_return_url(
        request, request.POST.get("reader_return", ""), evidence=evidence
    )
    return redirect(reader_url or source_url)


@product_login_required
@require_POST
def mark_evidence(request, case_id, kind, evidence_id):
    return _mark_response(
        request, set_evidence_mark, case_id, kind, evidence_id, evidence=True
    )


@product_login_required
@require_POST
def mark_finding(request, case_id, run_id, item_index):
    return _mark_response(request, set_finding_mark, case_id, run_id, item_index)


@product_login_required
@require_POST
def mark_reply(request, case_id, turn_id):
    return _mark_response(request, set_reply_mark, case_id, turn_id)


@product_login_required
@require_http_methods(["GET", "HEAD", "POST"])
def notes(request, case_id, item_id):
    if request.method == "POST":
        form = ImportantNoteForm(request.POST)
        case, item, target, note_count = submit_note(
            request.user, case_id, item_id, form
        )
    else:
        case = get_case(request.user, case_id)
        item, target = read_note_item(case, item_id)
        form = ImportantNoteForm()
        note_count = None
    important_page = page_number(request.GET.get("important_page", ""))
    from_list = item.marked_at is not None and "important_page" in request.GET
    params = {"important_page": important_page} if from_list else {}
    reader_url = reader_return_url(
        request,
        request.GET.get("reader_return", ""),
        evidence=item.evidence_id is not None,
    )
    if reader_url:
        params["reader_return"] = reader_return_token(request.path, reader_url)
    source_url = reader_url or target["url"]
    if from_list:
        source_url = with_return(
            source_url,
            {"important_page": important_page, "important_item": str(item.pk)},
        )
    if note_count is not None:
        return form_saved(
            request,
            "Note added.",
            reverse("core:important:notes", args=[case.pk, item.pk], query=params),
            note_count=note_count,
        )

    page = Paginator(item.notes.select_related("author"), PER_PAGE).get_page(
        page_number(request.GET.get("page", ""))
    )
    notes_params = "&" + urlencode(params) if params else ""
    return render_quick_form(
        request,
        "core/important/notes.html",
        case_context(case, request.user)
        | {
            "item": item,
            "target": target,
            "form": form,
            "page_obj": page,
            "notes_params": notes_params,
            "back_url": list_url(case, page=important_page, item_id=item.pk)
            if from_list
            else "",
            "source_url": source_url,
        },
    )

from collections import defaultdict
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.catalogue import raw_finding_item
from core.analysis.important import finding_target
from core.analysis.services import SourceResolutionCache
from core.evidence.queries import get_evidence, scoped_evidence
from core.evidence.types import KIND_LABELS
from core.evidence_chat.services import reply_available
from core.models import AnalysisResult, ImportantItem, ImportantNote
from core.models.chat import ChatTurn
from phone.presentation import message_labels
from ui.dialogs import form_saved, render_quick_form
from ui.live import render_live

from .navigation import list_url, page_number, with_return
from .reader import reader_return_token, reader_return_url

PER_PAGE = 25
RESULT_RELATION = "finding_result__run__previous_run"


class ImportantNoteForm(forms.Form):
    body = forms.CharField(
        label="Note", max_length=4000, widget=forms.Textarea(attrs={"rows": 4})
    )


def _evidence_target(case, kind, evidence, *, title=None):
    return {
        "title": title or evidence.title,
        "label": KIND_LABELS[kind],
        "qualifiers": ["Archived"] if evidence.is_archived else [],
        "url": reverse("core:evidence:detail", args=[case.pk, kind, evidence.pk]),
    }


def _read_target(case, item):
    if item.chat_turn_id:
        return _reply_target(case, item.chat_turn)
    if item.finding_result_id:
        return finding_target(case, item.finding_result, item.item_index)
    evidence = get_evidence(case, item.evidence_kind, item.evidence_id)
    title = None
    if item.evidence_kind == "text-message":
        title = message_labels(case, [evidence.pk]).get(evidence.pk)
    return _evidence_target(case, item.evidence_kind, evidence, title=title)


def _reply_target(case, turn):
    if (
        turn.case_id != case.pk
        or turn.chat.case_id != case.pk
        or not reply_available(turn)
    ):
        raise Http404("Reply unavailable.")
    return {
        "title": turn.question,
        "label": "Evidence chat reply",
        "url": reverse("core:chat:reply", args=[case.pk, turn.chat_id, turn.pk]),
    }


def _page_rows(case, page):
    evidence_ids = defaultdict(list)
    for item in page:
        if item.evidence_kind in KIND_LABELS:
            evidence_ids[item.evidence_kind].append(item.evidence_id)
    evidence = {
        (kind, record.pk): record
        for kind, ids in evidence_ids.items()
        for record in scoped_evidence(case, kind)
        .filter(pk__in=ids)
        .only("title", "is_archived")
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
                target = _reply_target(case, item.chat_turn)
            elif item.finding_result_id:
                if item.finding_result_id != cached_result_id:
                    # Avoid keeping full source text from several results in memory.
                    cached_result_id = item.finding_result_id
                    source_cache = SourceResolutionCache(case)
                target = finding_target(
                    case,
                    item.finding_result,
                    item.item_index,
                    finding=item.raw_item,
                    cache=source_cache,
                )
            else:
                original = evidence[(item.evidence_kind, item.evidence_id)]
                target = _evidence_target(
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
    items = (
        ImportantItem.objects.filter(case=case, marked_at__isnull=False)
        .select_related("marked_by", "chat_turn__chat", RESULT_RELATION)
        .defer("finding_result__data", "finding_result__provenance")
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


def _apply_mark(request, case, target, source_url):
    action = request.POST.get("action")
    if action == "mark":
        item, _ = ImportantItem.objects.get_or_create(case=case, **target)
        if item.marked_at is None:
            item.marked_at = timezone.now()
            item.marked_by = request.user
            item.save(update_fields=("marked_at", "marked_by"))
        messages.success(request, "Marked important.")
    elif action == "unmark":
        ImportantItem.objects.filter(case=case, **target).update(
            marked_at=None, marked_by=None
        )
        messages.success(request, "Removed from Important.")
    else:
        return HttpResponseBadRequest("Choose whether to mark or remove this item.")
    reader_url = reader_return_url(
        request, request.POST.get("reader_return", ""), evidence="evidence_id" in target
    )
    return redirect(reader_url or source_url)


@product_login_required
@require_POST
@transaction.atomic
def mark_evidence(request, case_id, kind, evidence_id):
    case = get_case(request.user, case_id, edit=True, lock=True)
    evidence = get_evidence(case, kind, evidence_id, lock=True, identity_only=True)
    return _apply_mark(
        request,
        case,
        {"evidence_kind": kind, "evidence_id": evidence.pk},
        reverse("core:evidence:detail", args=[case.pk, kind, evidence.pk]),
    )


@product_login_required
@require_POST
@transaction.atomic
def mark_finding(request, case_id, run_id, item_index):
    case = get_case(request.user, case_id, edit=True, lock=True)
    result = get_object_or_404(
        AnalysisResult.objects.select_related("run"),
        case=case,
        run__case=case,
        run_id=run_id,
    )
    target = finding_target(case, result, item_index)
    return _apply_mark(
        request,
        case,
        {"finding_result": result, "item_index": item_index},
        target["url"],
    )


@product_login_required
@require_POST
@transaction.atomic
def mark_reply(request, case_id, turn_id):
    case = get_case(request.user, case_id, edit=True, lock=True)
    turn = get_object_or_404(
        ChatTurn.objects.select_related("chat"), pk=turn_id, case=case, chat__case=case
    )
    target = _reply_target(case, turn)
    return _apply_mark(request, case, {"chat_turn": turn}, target["url"])


@product_login_required
@require_http_methods(["GET", "HEAD", "POST"])
@transaction.atomic
def notes(request, case_id, item_id):
    writing = request.method == "POST"
    case = get_case(request.user, case_id, edit=writing, lock=writing)
    item = get_object_or_404(
        ImportantItem.objects.select_related("chat_turn__chat", RESULT_RELATION),
        case=case,
        pk=item_id,
    )
    target = _read_target(case, item)
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
    form = ImportantNoteForm(request.POST if writing else None)
    if writing:
        if item.marked_at is None:
            raise PermissionDenied("Mark this item important to add a note.")
        if form.is_valid():
            ImportantNote.objects.create(
                item=item, author=request.user, body=form.cleaned_data["body"]
            )
            return form_saved(
                request,
                "Note added.",
                reverse("core:important:notes", args=[case.pk, item.pk], query=params),
                note_count=item.notes.count(),
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

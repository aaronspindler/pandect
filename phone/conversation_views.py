"""Read retained messages together without inventing native thread identity."""

from urllib.parse import urlencode
from uuid import UUID

from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Q
from django.db.models.functions import Greatest, Least
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.evidence.queries import local_date_range, scoped_evidence
from people.models import Person
from phone.conversation_analysis import (
    CONTEXT_PARAMETERS,
    conversation_analysis_context,
)
from phone.conversation_sessions import attach_session_groups
from phone.conversations import (
    conversation_list_page,
    imported_contexts,
    matching_copy_groups,
    pair_messages,
    preceding_messages,
    timeline_messages,
)
from phone.forms import ConversationDayForm, ConversationSearchForm
from phone.instance_views import conversation_instances

MESSAGES_PER_PAGE = 50


@product_login_required
@require_safe
def conversation_list(request, case_id):
    case = get_case(request.user, case_id)
    pairs = (
        scoped_evidence(case, "text-message")
        .filter(is_archived=False, recipient_id__isnull=False)
        .order_by()
        .annotate(
            first_id=Least("sender_id", "recipient_id"),
            second_id=Greatest("sender_id", "recipient_id"),
        )
        .values("first_id", "second_id")
        .annotate(message_count=Count("pk"), last_recorded_at=Max("occurred_at"))
        .order_by(F("last_recorded_at").desc(nulls_last=True), "first_id", "second_id")
    )
    page = Paginator(pairs, 25).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    people = Person.objects.filter(
        case=case,
        pk__in={row[key] for row in rows for key in ("first_id", "second_id")},
    ).in_bulk()
    for row in rows:
        row["first_person"] = people[row["first_id"]]
        row["second_person"] = people[row["second_id"]]
        row["url"] = reverse(
            "phone:conversation_detail",
            kwargs={
                "case_id": case.pk,
                "first_person_id": row["first_id"],
                "second_person_id": row["second_id"],
            },
        )
        if page.number > 1:
            row["url"] += "?" + urlencode({"list_page": page.number})
    page.object_list = rows
    return render(
        request,
        "phone/conversation_list.html",
        case_context(case, request.user)
        | {"page_obj": page, "nav_section": "analysis"},
    )


@product_login_required
@require_safe
def conversation_detail(request, case_id, first_person_id, second_person_id):
    case = get_case(request.user, case_id)
    first = get_object_or_404(Person, case=case, pk=first_person_id)
    second = get_object_or_404(Person, case=case, pk=second_person_id)
    if first.pk > second.pk:
        url = reverse(
            "phone:conversation_detail",
            kwargs={
                "case_id": case.pk,
                "first_person_id": second.pk,
                "second_person_id": first.pk,
            },
        )
        params = {
            key: request.GET[key]
            for key in (
                *CONTEXT_PARAMETERS,
                "focus",
                "tab",
                "instance",
                "full_summary",
                "message_page",
                "copies",
                "copy_page",
                "day",
            )
            if request.GET.get(key)
        }
        return redirect(url + (f"?{urlencode(params)}" if params else ""))

    day_form = ConversationDayForm(request.GET if "day" in request.GET else None)
    day = day_form.cleaned_data["day"] if day_form.is_valid() else None
    if request.GET.get("tab") == "instances":
        return conversation_instances(request, case, first, second, day_form, day)

    search_form = ConversationSearchForm(request.GET if "q" in request.GET else None)
    query = search_form.cleaned_data["q"] if search_form.is_valid() else ""

    pair = (first, second)
    messages = pair_messages(case, *pair)
    contexts = imported_contexts(case, pair=pair)
    groups = matching_copy_groups(contexts)
    timeline = timeline_messages(messages, contexts, groups)
    visible_timeline = timeline.select_related("sender", "recipient")
    if query:
        match_body = Q(body__icontains=query)
        hits = timeline.filter(match_body).only("occurred_at")
        visible_timeline = visible_timeline.annotate(matches_search=match_body)
    selected = None
    selected_value = request.GET.get("message")
    target_value = selected_value or request.GET.get("focus")
    if target_value:
        try:
            message_id = UUID(target_value)
        except ValueError:
            raise Http404("Message not found.") from None
        selected = get_object_or_404(
            messages.select_related("sender", "recipient"), pk=message_id
        )

    day_message = None
    match_page = None
    match_message = None
    for _attempt in range(2):
        anchor = None
        paginator = Paginator(visible_timeline, MESSAGES_PER_PAGE)
        page_number = request.GET.get("page")
        if query:
            match_page = Paginator(hits, 1).get_page(request.GET.get("match", 1))
            match_message = next(iter(match_page), None)
        jump_to_match = match_message is not None and not (
            selected or day is not None or page_number
        )
        if selected:
            # Rank the logical row, even when the URL names another retained copy.
            native_id = contexts.filter(message=selected).values("native_message_id")
            selected_group = (
                groups.filter(native_message_id__in=native_id)
                .order_by("native_message_id")
                .first()
            )
            representative = (
                selected_group["representative"] if selected_group else selected.pk
            )
            page_number = (
                preceding_messages(timeline, selected, row_id=representative).count()
                // MESSAGES_PER_PAGE
                + 1
            )
        elif day is not None:
            day_message = (
                timeline.filter(**local_date_range("occurred_at", start=day, end=day))
                .only("occurred_at")
                .first()
            )
            anchor = day_message
        elif jump_to_match:
            anchor = match_message
        if anchor is not None:
            page_number = (
                preceding_messages(timeline, anchor, row_id=anchor.pk).count()
                // MESSAGES_PER_PAGE
                + 1
            )
        page = paginator.get_page(page_number)
        rows = list(page.object_list)
        for message in rows:
            message.timeline_id = message.pk
        if selected is None:
            if anchor is not None and all(row.pk != anchor.pk for row in rows):
                continue
            break
        target = next(
            (i for i, row in enumerate(rows) if row.pk == representative), None
        )
        if target is not None:
            if selected.pk == representative:
                selected = rows[target]
            else:
                # Recheck membership before displaying a different original.
                current = (
                    groups.filter(native_message_id__in=native_id)
                    .order_by("native_message_id")
                    .first()
                )
                if not current or current["representative"] != representative:
                    continue
                selected.timeline_id = representative
                if query:
                    selected.matches_search = rows[target].matches_search
                rows[target] = selected
            break
    else:
        raise Http404("Message unavailable. Reopen the conversation and try again.")

    visible_contexts = {
        context.message_id: context
        for context in imported_contexts(
            case, pair=pair, message_ids=[row.pk for row in rows]
        )
        .select_related("source_document")
        .only("message_id", "source_document__title", "source_row", "native_message_id")
    }
    visible_groups = groups.filter(
        native_message_id__in={c.native_message_id for c in visible_contexts.values()}
    )
    group_sizes = {g["representative"]: g["copy_count"] for g in visible_groups}
    list_page = conversation_list_page(request.GET.get("list_page"))
    search_params = {"list_page": list_page} if list_page > 1 else {}
    if request.GET.get("weak") == "1":
        search_params["weak"] = "1"
    if query:
        search_params.update(q=query, match=match_page.number)
    evidence_params = {"conversation_list_page": list_page} if list_page > 1 else {}
    for message in rows:
        message.copy_count = group_sizes.get(message.timeline_id, 1)
        message.imported_source = visible_contexts.get(message.pk)
        evidence_args = (case.pk, "text-message", message.pk)
        message.evidence_url = reverse(
            "core:evidence:detail", args=evidence_args, query=evidence_params
        )
        if message.imported_source:
            message.source_info_url = reverse(
                "core:evidence:detail",
                args=evidence_args,
                query=evidence_params | {"info": "1"},
                fragment="evidence-details-heading",
            )
            if message.copy_count > 1 and request.GET.get("copies") == str(
                message.timeline_id
            ):
                confirmed = groups.filter(
                    native_message_id=message.imported_source.native_message_id
                )
                copies = (
                    contexts.filter(
                        native_message_id__in=confirmed.values("native_message_id")
                    )
                    .select_related("source_document")
                    .only("source_document__title", "source_row", "message_id")
                    .order_by(
                        "source_document__title", "source_document_id", "source_row"
                    )
                )
                message.copy_page = Paginator(copies, 25).get_page(
                    request.GET.get("copy_page")
                )

    page.object_list = rows
    params = request.GET.copy()
    params.pop("q", None)
    params.pop("match", None)
    params.update(search_params)
    if anchor is not None or (target_value and not selected_value):
        params["page"] = page.number
    if not selected_value:
        selected = None
    list_url = reverse(
        "phone:conversation_list",
        args=(case.pk,),
        query={"page": list_page} if list_page > 1 else {},
    )
    context = case_context(case, request.user) | {
        "first_person": first,
        "second_person": second,
        "page_obj": page,
        "selected_message": selected,
        "day_form": day_form,
        "day_message": day_message,
        "day_not_found": day is not None and day_message is None and selected is None,
        "search_form": search_form,
        "search_query": query,
        "search_params": search_params,
        "match_page": match_page,
        "match_message": match_message,
        "match_in_page": match_message is not None
        and any(row.timeline_id == match_message.pk for row in rows),
        "conversation_list_url": list_url,
        "list_page": list_page,
        "transcript_query": {
            key: params[key] for key in CONTEXT_PARAMETERS if params.get(key)
        },
        "page_query": urlencode(search_params),
        "nav_section": "analysis",
    }
    context.update(
        conversation_analysis_context(
            case,
            messages,
            rows,
            selected=selected,
            params=params,
            list_page=list_page,
        )
    )
    attach_session_groups(case, rows)
    return render(request, "phone/conversation_detail.html", context)

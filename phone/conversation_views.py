"""Read retained messages together without inventing native thread identity."""

from urllib.parse import urlencode
from uuid import UUID

from django.core.paginator import Paginator
from django.db.models import Count, F, Max
from django.db.models.functions import Greatest, Least
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.evidence.queries import get_evidence, scoped_evidence
from document.models import DocumentEvidence
from people.models import Person
from phone.conversation_analysis import conversation_analysis_context
from phone.conversations import (
    conversation_list_page,
    imported_contexts,
    pair_messages,
    preceding_messages,
)


@product_login_required
@require_safe
def conversation_list(request, case_id):
    case = get_case(request.user, case_id)
    pairs = (
        scoped_evidence(case, "text-message")
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
                "source",
                "page",
                "message",
                "list_page",
                "analysis_page",
                "conversation_analysis_page",
            )
            if request.GET.get(key)
        }
        return redirect(url + (f"?{urlencode(params)}" if params else ""))

    messages = pair_messages(case, first, second)
    contexts = imported_contexts(case).filter(message__in=messages)
    sources = list(
        DocumentEvidence.objects.filter(
            case=case, pk__in=contexts.values("source_document_id")
        ).order_by("title", "pk")
    )
    source = None
    if request.GET.get("source"):
        try:
            source_id = UUID(request.GET["source"])
        except ValueError:
            raise Http404("Export not found.") from None
        source = get_evidence(case, "document", source_id)
        if source.pk not in {document.pk for document in sources}:
            sources.append(source)
        messages = messages.filter(
            communication_context__case=case,
            communication_context__source_document=source,
            communication_context__source_document__case=case,
        )
    messages = messages.order_by(F("occurred_at").asc(nulls_last=True), "pk")
    selected = None
    if request.GET.get("message"):
        try:
            message_id = UUID(request.GET["message"])
        except ValueError:
            raise Http404("Message not found.") from None
        selected = get_object_or_404(messages, pk=message_id)
    paginator = Paginator(messages.select_related("sender", "recipient"), 50)
    # An import may move the requested message across a page boundary between
    # counting and reading. Retry once, and never substitute another message.
    for _attempt in range(2):
        page_number = (
            preceding_messages(messages, selected).count() // 50 + 1
            if selected
            else request.GET.get("page")
        )
        page = paginator.get_page(page_number)
        rows = list(page.object_list)
        if selected is None:
            break
        match = next((message for message in rows if message.pk == selected.pk), None)
        if match:
            selected = match
            break
        paginator = Paginator(messages.select_related("sender", "recipient"), 50)
    else:
        raise Http404("Message unavailable. Reopen the conversation and try again.")
    visible_contexts = {
        context.message_id: context
        for context in imported_contexts(case)
        .filter(message_id__in=[message.pk for message in rows])
        .select_related("source_document")
    }
    list_page = conversation_list_page(request.GET.get("list_page"))
    source_params = {"source": str(source.pk)} if source else {}
    if list_page > 1:
        source_params["list_page"] = list_page
    for message in rows:
        message.evidence_url = reverse(
            "core:evidence:detail", args=(case.pk, "text-message", message.pk)
        )
        evidence_params = {}
        if source:
            evidence_params["conversation_source"] = str(source.pk)
        if list_page > 1:
            evidence_params["conversation_list_page"] = list_page
        if evidence_params:
            message.evidence_url += "?" + urlencode(evidence_params)
        message.imported_source = visible_contexts.get(message.pk)
    page.object_list = rows
    list_url = reverse("phone:conversation_list", args=(case.pk,))
    if list_page > 1:
        list_url += "?" + urlencode({"page": list_page})
    context = case_context(case, request.user) | {
        "first_person": first,
        "second_person": second,
        "page_obj": page,
        "selected_message": selected,
        "source_documents": sources,
        "conversation_list_url": list_url,
        "list_page": list_page,
        "selected_source": source,
        "page_query": urlencode(source_params),
        "nav_section": "analysis",
    }
    context.update(
        conversation_analysis_context(
            case,
            messages,
            rows,
            selected=selected,
            params=request.GET,
            source=source,
            list_page=list_page,
        )
    )
    return render(request, "phone/conversation_detail.html", context)

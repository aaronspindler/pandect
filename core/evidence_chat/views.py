"""Case-scoped, progressively enhanced readers for saved evidence questions."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import (
    require_http_methods,
    require_POST,
    require_safe,
)

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import execution_confirmation, execution_destination
from core.important.reader import reader_context
from core.models.chat import ChatTurn, EvidenceChat
from ui.live import render_live

from . import ai, services
from .forms import QuestionForm


def _route_context(case):
    try:
        execution, revision = ai.turn_execution(case)
    except AnalysisError as exc:
        return {"unavailable": error_message(exc.code), "route_token": ""}
    return {
        "selected_route": (execution, revision),
        "route_token": execution_confirmation(
            "chat.submit", [str(case.pk), revision], execution
        ),
        "external_destination": execution_destination(execution)
        if execution["location"] == "external"
        else "",
    }


def _row(request, case, turn):
    url = reverse("core:chat:reply", args=[case.pk, turn.chat_id, turn.pk])
    row = {"turn": turn, "url": url}
    if turn.status == ChatTurn.Status.FAILED:
        row["error"] = error_message(turn.error_code)
    if turn.status != ChatTurn.Status.COMPLETE:
        return row
    answer = services.inspect_turn(turn)
    if answer is None:
        return row | {"withheld": True}
    references = []
    claims = []
    for claim in answer.get("claims", ()):
        numbers = []
        for citation in claim["citations"]:
            if citation not in references:
                references.append(citation)
            numbers.append(
                {"number": references.index(citation) + 1, "citation": citation}
            )
        claims.append({"text": claim["text"], "references": numbers})
    return row | {
        "answer": answer,
        "claims": claims,
        "references": references,
        "important": reader_context(request, case, source_url=url, chat_turn=turn),
    }


@product_login_required
@require_http_methods(["GET", "HEAD", "POST"])
def chat(request, case_id, chat_id=None):
    writing = request.method == "POST"
    case = get_case(request.user, case_id, edit=writing)
    thread = get_object_or_404(EvidenceChat, case=case, pk=chat_id) if chat_id else None
    context = case_context(case, request.user)
    if context["can_edit"]:
        context.update(_route_context(case))
        form = QuestionForm(
            request.POST if writing else None, initial={"route": context["route_token"]}
        )
        if writing and form.is_valid():
            try:
                if (
                    not context["route_token"]
                    or form.cleaned_data["route"] != context["route_token"]
                ):
                    raise ValidationError(
                        "The processing destination changed. "
                        "Review the current setting and submit again."
                    )
                turn = services.submit_turn(
                    request.user,
                    case.pk,
                    form.cleaned_data["question"],
                    submission_id=form.cleaned_data["submission_id"],
                    chat_id=chat_id,
                    include_archived=form.cleaned_data["include_archived"],
                    expected_route=context["selected_route"],
                )
            except (AnalysisError, ValidationError) as exc:
                form.add_error(
                    None,
                    error_message(exc.code) if isinstance(exc, AnalysisError) else exc,
                )
            else:
                return redirect(
                    reverse(
                        "core:chat:detail",
                        args=[case.pk, turn.chat_id],
                        fragment=f"turn-{turn.pk}",
                    )
                )
        if writing:
            # Preserve the question with a fresh destination on a corrected submission.
            form.data = form.data.copy()
            form.data["route"] = context["route_token"]
        context["form"] = form
    if thread is None:
        chats = (
            EvidenceChat.objects.filter(case=case)
            .annotate(last_asked_at=Max("turns__created_at"))
            .order_by("-last_asked_at", "-pk")
        )
        context["page_obj"] = Paginator(chats, 25).get_page(request.GET.get("page"))
        return render(request, "core/evidence_chat/index.html", context)
    turns = (
        thread.turns.filter(case=case).select_related("asked_by").order_by("ordinal")
    )
    paginator = Paginator(turns, 10)
    page = paginator.get_page(request.GET.get("page") or paginator.num_pages)
    rows = [_row(request, case, turn) for turn in page]
    context.update(
        thread=thread,
        rows=rows,
        page_obj=page,
        pending=turns.filter(
            status__in=(ChatTurn.Status.QUEUED, ChatTurn.Status.RUNNING)
        ).exists(),
    )
    return render_live(request, "core/evidence_chat/detail.html", context)


def _get_turn(case, chat_id, turn_id):
    return get_object_or_404(
        ChatTurn.objects.select_related("chat", "asked_by"),
        pk=turn_id,
        case=case,
        chat_id=chat_id,
        chat__case=case,
    )


@product_login_required
@require_safe
def reply(request, case_id, chat_id, turn_id):
    case = get_case(request.user, case_id)
    turn = _get_turn(case, chat_id, turn_id)
    row = _row(request, case, turn)
    context = case_context(case, request.user) | {
        "thread": turn.chat,
        "turn": turn,
        "row": row,
        "pending": turn.status in (ChatTurn.Status.QUEUED, ChatTurn.Status.RUNNING),
    }
    if "answer" in row:
        if turn.mode == "full":
            context["matches_page"] = services.match_page(
                turn, request.GET.get("page", 1)
            )
        if turn.gaps:
            context["gaps_page"] = services.gap_page(
                turn, request.GET.get("gaps_page", 1)
            )
    if context["can_edit"] and turn.status == ChatTurn.Status.FAILED:
        context.update(_route_context(case))
    return render_live(request, "core/evidence_chat/reply.html", context)


@product_login_required
@require_POST
def retry(request, case_id, chat_id, turn_id):
    case = get_case(request.user, case_id, edit=True)
    turn = _get_turn(case, chat_id, turn_id)
    route = _route_context(case)
    try:
        if (
            not route["route_token"]
            or request.POST.get("route") != route["route_token"]
        ):
            raise ValidationError(
                "Review the current processing destination before retrying."
            )
        turn = services.retry_turn(
            request.user,
            case.pk,
            chat_id,
            turn_id,
            expected_route=route["selected_route"],
        )
    except (AnalysisError, ValidationError) as exc:
        messages.error(
            request,
            error_message(exc.code)
            if isinstance(exc, AnalysisError)
            else " ".join(exc.messages),
        )
    return redirect("core:chat:reply", case.pk, chat_id, turn.pk)

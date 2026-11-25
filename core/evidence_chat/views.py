"""Case-scoped, progressively enhanced readers for saved evidence questions."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery
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

from . import services
from .forms import QuestionForm
from .jobs import review_restart_code


def _route_context(case, *, turn=None):
    if turn is not None and turn.include_archived:
        return {
            "unavailable": error_message("chat_archive_scope_retired"),
            "route_token": "",
        }
    if turn is not None and (code := review_restart_code(turn)):
        return {
            "unavailable": error_message(code),
            "route_token": "",
        }
    try:
        selected = services.submission_route(case, turn=turn)
    except AnalysisError as exc:
        return {"unavailable": error_message(exc.code), "route_token": ""}
    execution, revision, search, ranking = selected
    return {
        "selected_route": selected,
        "route_token": execution_confirmation(
            "chat.submit", [str(case.pk), revision, search, ranking], execution
        ),
        "ranking_destination": ranking
        if ranking.get("provider") == "workers-ai"
        else {},
        "embedding_destination": search.get("destination"),
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
        code = review_restart_code(turn)
        return row | {
            "withheld": error_message(code)
            if code
            else "Reply unavailable because its sources could not be verified."
        }
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
    latest_turn = ChatTurn.objects.filter(case=case, chat_id=OuterRef("pk"))
    chats = (
        EvidenceChat.objects.filter(case=case)
        .annotate(
            last_asked_at=Subquery(
                latest_turn.order_by("-created_at").values("created_at")[:1]
            )
        )
        .order_by("-last_asked_at", "-pk")
    )
    if thread is None:
        context["page_obj"] = Paginator(chats, 25).get_page(request.GET.get("page"))
        return render(request, "core/evidence_chat/index.html", context)
    turns = (
        thread.turns.filter(case=case)
        .select_related("asked_by", "case")
        .order_by("ordinal")
    )
    paginator = Paginator(turns, 10)
    page = paginator.get_page(request.GET.get("page") or paginator.num_pages)
    rows = [_row(request, case, turn) for turn in page]
    context.update(
        thread=thread,
        rows=rows,
        page_obj=page,
        recent_chats=chats[:10],
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
        context.update(_route_context(case, turn=turn))
    return render_live(request, "core/evidence_chat/reply.html", context)


@product_login_required
@require_POST
def retry(request, case_id, chat_id, turn_id):
    case = get_case(request.user, case_id, edit=True)
    turn = _get_turn(case, chat_id, turn_id)
    route = _route_context(case, turn=turn)
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

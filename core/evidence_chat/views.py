"""Case-scoped, progressively enhanced readers for saved evidence questions."""

from uuid import uuid4

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery
from django.http import Http404
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

from . import activity, services
from .forms import QuestionForm
from .search_scope import review_restart_code


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
    execution, revision = selected
    return {
        "selected_route": selected,
        "route_token": execution_confirmation(
            "chat.submit", [str(case.pk), revision], execution
        ),
        "external_destination": execution_destination(execution),
    }


def _row(request, case, turn):
    url = reverse("core:chat:reply", args=[case.pk, turn.chat_id, turn.pk])
    row = {"turn": turn, "url": url}
    if (
        turn.review_of_id
        and turn.review_of.case_id == case.pk
        and turn.review_of.chat_id == turn.chat_id
    ):
        row["deep_origin"] = reverse(
            "core:chat:reply", args=(case.pk, turn.chat_id, turn.review_of_id)
        )

    parent_check = getattr(turn, "approved_check", None)
    if (
        parent_check
        and parent_check.parent_id != turn.pk
        and parent_check.parent.case_id == case.pk
        and parent_check.parent.chat_id == turn.chat_id
    ):
        row["origin_reply"] = reverse(
            "core:chat:reply", args=[case.pk, turn.chat_id, parent_check.parent_id]
        )
    if turn.status == ChatTurn.Status.FAILED:
        row["error"] = error_message(turn.error_code)
    if turn.status != ChatTurn.Status.COMPLETE:
        return row | {"activity": activity.describe(turn)}
    answer = services.inspect_turn(turn)
    if answer is None:
        code = review_restart_code(turn)
        return row | {
            "withheld": error_message(code)
            if code
            else "Reply unavailable because its sources could not be verified."
        }
    check = getattr(turn, "analysis_proposal", None)
    if (
        check
        and check.document.case_id == case.pk
        and (not check.run_id or check.run.case_id == case.pk)
    ):
        row["proposal"] = check
        if (
            check.continuation_id
            and check.continuation_id != turn.pk
            and check.continuation.case_id == case.pk
            and check.continuation.chat_id == turn.chat_id
        ):
            row["linked_reply"] = reverse(
                "core:chat:reply", args=[case.pk, turn.chat_id, check.continuation_id]
            )
    if turn.plan.get("agent_version"):
        row["update_submission"] = uuid4()
        from .checkpoints import evidence_versions

        row["newer_material"] = turn.plan.get("cutoff_versions") != evidence_versions(
            case
        )
    references = []
    claims = []
    support = turn.plan.get("claim_support", [])
    for index, claim in enumerate(answer.get("claims", ())):
        numbers = []
        for citation in claim["citations"]:
            if citation not in references:
                references.append(citation)
            numbers.append(
                {"number": references.index(citation) + 1, "citation": citation}
            )
        claims.append(
            {
                "text": claim["text"],
                "references": numbers,
                "support": support[index] if index < len(support) else None,
            }
        )
    return row | {
        "answer": answer,
        "activity": activity.describe(turn),
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
        .select_related(
            "asked_by",
            "case",
            "deeper_review",
            "review_of",
            "analysis_proposal__document",
            "analysis_proposal__run",
            "analysis_proposal__continuation",
            "approved_check__parent",
        )
        .order_by("ordinal")
    )
    paginator = Paginator(turns, 10)
    page = paginator.get_page(request.GET.get("page") or paginator.num_pages)
    rows = [_row(request, case, turn) for turn in page]
    active_turn = turns.filter(
        status__in=(ChatTurn.Status.QUEUED, ChatTurn.Status.RUNNING)
    ).exists()
    context.update(
        thread=thread,
        rows=rows,
        page_obj=page,
        recent_chats=chats[:10],
        active_turn=active_turn,
        pending=active_turn
        or any(
            row.get("proposal")
            and row["proposal"].status == "approved"
            and not row["proposal"].continuation_id
            for row in rows
        ),
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
def selection(request, case_id, chat_id, turn_id):
    from django.db import transaction

    from . import selections

    with transaction.atomic():
        case = get_case(request.user, case_id, lock=True)
        turn = _get_turn(case, chat_id, turn_id)
        if (
            not turn.plan.get("recorded_selection")
            or services.inspect_turn(turn) is None
        ):
            raise Http404("Matching records unavailable.")
        group = request.GET.get("group", "")
        groupings = selections.grouping_choices(turn.plan["recorded_selection"])
        if group and group not in groupings:
            raise Http404("Unknown selection grouping.")
        page = selections.page(turn, request.GET.get("page", 1), group)
        if page is None:
            raise Http404("Matching records unavailable.")
        return render(
            request,
            "core/evidence_chat/selection.html",
            case_context(case, request.user)
            | {
                "thread": turn.chat,
                "turn": turn,
                "page": page,
                "selection": turn.plan["recorded_selection"],
                "group": group,
                "groupings": groupings,
                "group_label": groupings.get(group, ""),
                "filters": selections.filter_labels(turn),
            },
        )


@product_login_required
@require_safe
def reply(request, case_id, chat_id, turn_id):
    from .usage import interaction_usage

    case = get_case(request.user, case_id)
    turn = _get_turn(case, chat_id, turn_id)
    row = _row(request, case, turn)
    context = case_context(case, request.user) | {
        "thread": turn.chat,
        "turn": turn,
        "row": row,
        "pending": turn.status in (ChatTurn.Status.QUEUED, ChatTurn.Status.RUNNING)
        or bool(
            row.get("proposal")
            and row["proposal"].status == "approved"
            and not row["proposal"].continuation_id
        ),
    }
    if "answer" in row:
        context["interaction_usage"] = interaction_usage(turn)
        if turn.mode == "full":
            context["matches_page"] = services.match_page(
                turn, request.GET.get("page", 1)
            )
        if turn.gaps:
            context["gaps_page"] = services.gap_page(
                turn, request.GET.get("gaps_page", 1)
            )
    if context["can_edit"] and (
        turn.status == ChatTurn.Status.FAILED or row.get("newer_material")
    ):
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


@product_login_required
@require_http_methods(["GET", "HEAD", "POST"])
def proposal(request, case_id, chat_id, turn_id):
    from core.models import ChatAnalysisProposal

    from . import proposals

    case = get_case(request.user, case_id, edit=True)
    turn = _get_turn(case, chat_id, turn_id)
    check = get_object_or_404(
        ChatAnalysisProposal.objects.select_related("document", "parent__chat"),
        parent=turn,
        document__case=case,
    )
    reply_url = reverse("core:chat:reply", args=(case.pk, chat_id, turn.pk))
    if check.status != "offered":
        return redirect(reply_url)
    unavailable = ""
    if request.method == "POST":
        try:
            proposals.respond(
                request.user,
                case_id,
                chat_id,
                turn_id,
                request.POST.get("action"),
                request.POST.get("confirmation", ""),
            )
        except (AnalysisError, ValidationError) as error:
            unavailable = (
                error_message(error.code)
                if isinstance(error, AnalysisError)
                else " ".join(error.messages)
            )
        else:
            return redirect(reply_url)
    if not unavailable:
        try:
            proposals.check_offer(check)
        except AnalysisError as error:
            unavailable = error_message(error.code)
    return render(
        request,
        "core/evidence_chat/proposal.html",
        case_context(case, request.user)
        | {
            "thread": turn.chat,
            "turn": turn,
            "proposal": check,
            "reply_url": reply_url,
            "unavailable": unavailable,
            "confirmation": proposals.reviewed_token(check, request.user),
            "external_destination": execution_destination(turn.execution)
            if turn.execution["location"] == "external"
            else "",
        },
        status=409 if unavailable and request.method == "POST" else 200,
    )


@product_login_required
@require_POST
def update_answer(request, case_id, chat_id, turn_id):
    case = get_case(request.user, case_id, edit=True)
    parent = _get_turn(case, chat_id, turn_id)
    route = _route_context(case)
    try:
        if (
            not route["route_token"]
            or request.POST.get("route") != route["route_token"]
        ):
            raise ValidationError("Review the current processing destination.")
        turn = services.submit_turn(
            request.user,
            case.pk,
            parent.question,
            submission_id=request.POST.get("submission_id"),
            chat_id=chat_id,
            expected_route=route["selected_route"],
            update_of=parent.pk,
        )
    except (AnalysisError, ValidationError) as exc:
        messages.error(
            request,
            error_message(exc.code)
            if isinstance(exc, AnalysisError)
            else " ".join(exc.messages),
        )
        return redirect("core:chat:reply", case.pk, chat_id, turn_id)
    return redirect("core:chat:reply", case.pk, chat_id, turn.pk)

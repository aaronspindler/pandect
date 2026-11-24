"""The summary-first view of saved sessions for one reported participant pair."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, F, OuterRef
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.execution import selected_ai_executions
from people.models import Person
from phone.analysis_forms import ConversationGroupingForm
from phone.conversation_instances import instance_page, instance_url
from phone.conversations import conversation_list_page, conversation_url, pair_messages
from phone.grouping_jobs import grouping_scope, start_grouping
from phone.saved_sessions import valid_sessions


def conversation_instances(request, case, first, second, day_form, day):
    list_page = conversation_list_page(request.GET.get("list_page"))
    containing = request.GET.get("contains")
    page = instance_page(
        case,
        pair_messages(case, first, second),
        request.GET.get("page"),
        instance_id=request.GET.get("instance"),
        containing=containing,
        message_page=request.GET.get("message_page"),
        day=day,
        full_summary_id=request.GET.get("full_summary"),
        summary_executions=selected_ai_executions(case, "session_summaries", {}),
    )
    day_message = None
    for instance in page:
        day_message = instance.get("day_message") or day_message
        if instance.get("messages") is not None:
            for message in instance["messages"]:
                message.transcript_url = conversation_url(
                    case, message, list_page=list_page
                )
    query = {"tab": "instances"}
    if list_page > 1:
        query["list_page"] = list_page
    if containing:
        query["contains"] = containing
    return render(
        request,
        "phone/conversation_detail.html",
        case_context(case, request.user)
        | {
            "first_person": first,
            "second_person": second,
            "instances_tab": True,
            "day_form": day_form,
            "day_message": day_message,
            "day_not_found": day is not None
            and day_message is None
            and not request.GET.get("instance"),
            "instance_query": query,
            "page_obj": page,
            "list_page": list_page,
            "conversation_list_url": reverse(
                "phone:conversation_list",
                args=(case.pk,),
                query={"page": list_page} if list_page > 1 else None,
            ),
            "nav_section": "analysis",
        },
    )


@product_login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def group_conversation(request, case_id, first_person_id, second_person_id):
    submitting = request.method == "POST"
    case = get_case(request.user, case_id, edit=True, lock=submitting)
    first = get_object_or_404(Person, case=case, pk=first_person_id)
    second = get_object_or_404(Person, case=case, pk=second_person_id)
    form = ConversationGroupingForm(request.POST if submitting else None)
    query = {"tab": "instances"}
    list_page = conversation_list_page(request.GET.get("list_page"))
    if list_page > 1:
        query["list_page"] = list_page
    back = reverse(
        "phone:conversation_detail",
        args=(case.pk, *sorted((first.pk, second.pk))),
        query=query,
    )
    if submitting and form.is_valid():
        try:
            _, created = start_grouping(
                request.user,
                case,
                first,
                second,
                gap_minutes=form.cleaned_data["gap_minutes"],
                reviewed_scope=form.cleaned_data["reviewed_scope"],
                submission_id=form.cleaned_data["submission_id"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        except AnalysisError as error:
            form.add_error(None, error_message(error.code))
        else:
            messages.success(
                request,
                "Conversation grouping queued. Follow its progress in Processing."
                if created
                else "This conversation grouping was already requested.",
            )
            return redirect(back)
    count, digest = grouping_scope(case, first, second)
    if form.is_bound:
        form.data = form.data.copy()
        form.data["reviewed_scope"] = digest
    else:
        form.initial["reviewed_scope"] = digest
    return render(
        request,
        "phone/conversation_group_review.html",
        case_context(case, request.user)
        | {
            "nav_section": "analysis",
            "first": first,
            "second": second,
            "back_url": back,
            "can_queue": count > 0,
            "sources_count": count,
            "form": form,
        },
        status=400 if submitting else 200,
    )


@product_login_required
@require_http_methods(["GET"])
def grouping_details(request, case_id, first_person_id, second_person_id, instance_id):
    case = get_case(request.user, case_id)
    first = get_object_or_404(Person, case=case, pk=first_person_id)
    second = get_object_or_404(Person, case=case, pk=second_person_id)
    session = get_object_or_404(
        valid_sessions(case, pair_messages(case, first, second)).select_related(
            "grouping"
        ),
        pk=instance_id,
    )
    members = session.members
    continuations = (
        members.exclude(continuation_reason="")
        .annotate(
            parent_available=Exists(
                members.filter(message_id=OuterRef("parent_message_id"))
            ),
            elapsed_minutes=F("elapsed_seconds") / 60.0,
        )
        .order_by("ordinal")
    )
    return render(
        request,
        "phone/grouping_details.html",
        case_context(case, request.user)
        | {
            "session": session,
            "first": first,
            "second": second,
            "back_url": instance_url(case, (first.pk, second.pk), session.pk),
            "page_obj": Paginator(continuations, 25).get_page(request.GET.get("page")),
            "nav_section": "analysis",
        },
    )

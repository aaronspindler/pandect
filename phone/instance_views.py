"""The summary-first view of saved sessions for one reported participant pair."""

from hashlib import sha256

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Length
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from cases.permissions import case_context, get_case
from config.access import product_login_required
from core.analysis.errors import AnalysisError, error_message
from core.analysis.services import start_run
from people.models import Person
from phone.analysis_forms import ConversationGroupingForm
from phone.conversation_instances import instance_page
from phone.conversations import conversation_list_page, conversation_url, pair_messages
from text.analysis.shared import MAX_SOURCES, MAX_TEXT_CHARS


def conversation_instances(request, case, first, second, day_form, day):
    list_page = conversation_list_page(request.GET.get("list_page"))
    page = instance_page(
        case,
        pair_messages(case, first, second),
        request.GET.get("page"),
        instance_id=request.GET.get("instance"),
        message_page=request.GET.get("message_page"),
        day=day,
        full_summary_id=request.GET.get("full_summary"),
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
    pair = pair_messages(case, first, second).filter(is_archived=False)
    max_sources = min(MAX_SOURCES, settings.PANDECT_ANALYSIS_MAX_SOURCES)
    sources = list(
        pair.order_by("pk").values_list("pk", "sha256", Length("body"))[
            : max_sources + 1
        ]
    )
    limit_message = ""
    if len(sources) > max_sources:
        limit_message = (
            f"This conversation has more than {max_sources} active messages."
        )
    elif sum(length for _, _, length in sources) > MAX_TEXT_CHARS:
        limit_message = (
            "This conversation exceeds the grouping limit of "
            f"{MAX_TEXT_CHARS:,} characters."
        )
    can_queue = bool(sources) and not limit_message
    scope = sha256("|".join(f"{pk}:{digest}" for pk, digest, _ in sources).encode())
    form = ConversationGroupingForm(
        request.POST if submitting else None,
        initial={"reviewed_scope": scope.hexdigest()},
    )
    query = {"tab": "instances"}
    list_page = conversation_list_page(request.GET.get("list_page"))
    if list_page > 1:
        query["list_page"] = list_page
    back = reverse(
        "phone:conversation_detail",
        args=(case.pk, *sorted((first.pk, second.pk))),
        query=query,
    )
    if can_queue and submitting and form.is_valid():
        try:
            _, created = start_run(
                request.user,
                case.pk,
                "sessions",
                [{"kind": "text-message", "id": str(pk)} for pk, _, _ in sources],
                parameters={"gap_minutes": form.cleaned_data["gap_minutes"]},
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
    return render(
        request,
        "phone/conversation_group_review.html",
        case_context(case, request.user)
        | {
            "nav_section": "analysis",
            "first": first,
            "second": second,
            "back_url": back,
            "can_queue": can_queue,
            "sources_count": len(sources),
            "limit_message": limit_message,
            "form": form,
        },
        status=400 if submitting else 200,
    )

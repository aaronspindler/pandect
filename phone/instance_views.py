"""The summary-first view of saved sessions for one reported participant pair."""

from django.shortcuts import render
from django.urls import reverse

from cases.permissions import case_context
from phone.conversation_instances import instance_page
from phone.conversations import conversation_list_page, conversation_url, pair_messages


def conversation_instances(request, case, first, second):
    list_page = conversation_list_page(request.GET.get("list_page"))
    page = instance_page(
        case,
        pair_messages(case, first, second),
        request.GET.get("page"),
        instance_id=request.GET.get("instance"),
        message_page=request.GET.get("message_page"),
    )
    for instance in page:
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

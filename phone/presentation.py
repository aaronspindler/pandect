from django.core.paginator import Paginator
from django.utils import timezone
from django.utils.formats import date_format

from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS
from phone.conversations import conversation_url
from phone.models import CommunicationContext


def message_excerpt(body):
    excerpt = " ".join(body[:80].split()) or "Text message"
    if len(body) > 80:
        excerpt += "…"
    return excerpt


def message_evidence_label(body, sender, occurred_at):
    timestamp = (
        date_format(timezone.localtime(occurred_at), "d M Y, H:i:s T")
        if occurred_at
        else "Time not recorded"
    )
    return f"{message_excerpt(body)} · {sender} · {timestamp}"


def reader_context(
    case,
    kind,
    item,
    *,
    conversation_list_page=1,
    native_thread_page=None,
    native_reply_page=None,
):
    context = {}
    if kind in CALL_KINDS:
        context["attachments"] = item.attachments.filter(case=case).select_related(
            "uploaded_by"
        )
    if kind == "text-message":
        context["display_title"] = message_evidence_label(
            item.body, item.sender, item.occurred_at
        )
        context["conversation_url"] = conversation_url(
            case, item, list_page=conversation_list_page
        )
        context["communication_context"] = (
            CommunicationContext.objects.filter(
                case=case, message=item, source_document__case=case
            )
            .select_related("source_document")
            .first()
        )
        imported = context["communication_context"]
        if imported:
            related = (
                CommunicationContext.objects.filter(
                    case=case,
                    source_document=imported.source_document,
                    message__in=scoped_evidence(case, "text-message"),
                )
                .select_related("message")
                .only("source_row", "message__title")
            )
            context["native_reply_candidates"] = (
                Paginator(
                    related.filter(native_message_id=imported.reply_to_id).order_by(
                        "source_row", "pk"
                    ),
                    25,
                ).get_page(native_reply_page)
                if imported.reply_to_id
                else []
            )
            context["native_thread_messages"] = (
                Paginator(
                    related.filter(native_thread_id=imported.native_thread_id).order_by(
                        "source_row", "pk"
                    ),
                    25,
                ).get_page(native_thread_page)
                if imported.native_thread_id
                else []
            )
    return context

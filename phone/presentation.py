from django.core.paginator import Paginator
from django.db.models.functions import Substr
from django.utils import timezone
from django.utils.formats import date_format

from core.evidence.queries import scoped_evidence
from core.evidence.types import CALL_KINDS
from phone.conversations import conversation_url
from phone.models import CommunicationContext


def call_party_labels(call, *, reported_only=False):
    """Display both parties without treating a source identifier as a person."""
    if call.import_row_id is None:
        return str(call.sender), str(call.recipient)
    row = call.import_row
    account = row.batch.source_label
    other = row.identifier or "Unknown caller"
    sender, recipient = (
        (other, account) if row.direction == "incoming" else (account, other)
    )
    if reported_only:
        return sender, recipient
    return (
        str(call.sender) if call.sender_id else sender,
        str(call.recipient) if call.recipient_id else recipient,
    )


def message_excerpt(body):
    excerpt = " ".join(body[:80].split()) or "Text message"
    if len(body) > 80:
        excerpt += "…"
    return excerpt


def message_destination(message):
    if message.recipient_id is None:
        return "Group chat"
    return str(message.recipient)


def message_evidence_label(body, sender, occurred_at):
    timestamp = (
        date_format(timezone.localtime(occurred_at), "d M Y, H:i:s T")
        if occurred_at
        else "Time not recorded"
    )
    return f"{message_excerpt(body)} · {sender} · {timestamp}"


def message_labels(case, message_ids):
    return {
        message.pk: message_evidence_label(
            message.label_body, message.sender, message.occurred_at
        )
        for message in scoped_evidence(case, "text-message")
        .filter(pk__in=message_ids)
        .select_related("sender")
        .only(
            "occurred_at",
            "sender__first_name",
            "sender__last_name",
            "sender__email",
            "sender__phone_number",
        )
        .annotate(label_body=Substr("body", 1, 81))
    }


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
        if item.import_row_id:
            context["call_import_row"] = item.import_row
            context["reported_sender"], context["reported_recipient"] = (
                call_party_labels(item, reported_only=True)
            )
            context["call_person_links"] = (
                item.import_row.person_links.filter(person__case=case)
                .select_related("linked_by", "person")
                .order_by("role")
            )
    if kind == "text-message":
        context["display_title"] = message_evidence_label(
            item.body, item.sender, item.occurred_at
        )
        if item.recipient_id and not item.is_archived:
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
            if item.group_source_id:
                context["group_chat_label"] = imported.source_context.get(
                    "raw_fields", {}
                ).get("Chat Session")
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

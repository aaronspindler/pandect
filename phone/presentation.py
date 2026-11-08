from phone.models import CommunicationContext


def reader_context(case, kind, item):
    context = {}
    if kind in ("phone-call", "facetime"):
        context["attachments"] = item.attachments.filter(case=case).select_related(
            "uploaded_by"
        )
    if kind == "text-message":
        context["communication_context"] = (
            CommunicationContext.objects.filter(
                case=case, message=item, source_document__case=case
            )
            .select_related("source_document")
            .first()
        )
        imported = context["communication_context"]
        if imported:
            related = CommunicationContext.objects.filter(
                case=case, source_document=imported.source_document, message__case=case
            )
            context["native_reply_candidates"] = (
                related.filter(native_message_id=imported.reply_to_id).select_related(
                    "message"
                )[:20]
                if imported.reply_to_id
                else []
            )
            context["native_thread_messages"] = (
                related.filter(native_thread_id=imported.native_thread_id)
                .select_related("message")
                .order_by("source_row")[:100]
                if imported.native_thread_id
                else []
            )
    return context

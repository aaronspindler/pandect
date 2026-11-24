"""Register individual originals that can supply meaning search passages."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from core.case_search.semantic_model import CHUNK_BYTES, profile


@receiver(post_save, sender="text.TextEvidence")
@receiver(post_save, sender="phone.TextMessageEvidence")
@receiver(post_save, sender="phone.CallEvidenceAttachment")
def new_original(sender, instance, created, raw=False, **kwargs):
    if raw or not created:
        return
    from .indexing import add_sources

    source = {
        "textevidence": "text",
        "textmessageevidence": "message",
        "callevidenceattachment": "attachment",
    }[sender._meta.model_name]
    if source == "attachment" and not instance.transcript:
        return
    if (
        source != "message"
        and len(
            (instance.body if source == "text" else instance.transcript)[
                : CHUNK_BYTES + 1
            ].encode()
        )
        > CHUNK_BYTES
    ):
        from .generation import published
        from .parent_index import index_parent

        index_parent.enqueue(
            str(instance.case_id),
            str(instance.uploaded_by_id),
            source,
            str(instance.pk),
        )
        published(instance.case_id, "briefs", source, instance.pk)
    if profile():
        add_sources(instance.case_id, source, [instance.pk])


@receiver(post_save, sender="phone.ConversationGrouping")
def published_grouping(sender, instance, update_fields=None, raw=False, **kwargs):
    if (
        not raw
        and instance.published_at
        and (not update_fields or "published_at" in update_fields)
    ):
        from .generation import published
        from .parent_index import index_children

        index_children.enqueue(
            str(instance.case_id),
            str(instance.published_by_id),
            "session",
            {"grouping_id": str(instance.pk)},
        )
        published(instance.case_id, "conversations", "grouping", instance.pk)

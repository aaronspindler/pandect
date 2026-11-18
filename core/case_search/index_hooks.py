"""Register committed sources on their owning write paths, including CSV batches."""

from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

# Imports save messages individually for their evidence integrity checks. Buffer
# only IDs from that transaction and write at most one work record per source.
_batch = ContextVar("case_search_message_batch", default=None)


@contextmanager
def collect_import_messages(case_id):
    buffer = []
    token = _batch.set(buffer)
    try:
        yield
        if buffer:
            from .indexing import add_sources

            add_sources(case_id, "message", buffer)
    finally:
        _batch.reset(token)


@receiver(post_save, sender="text.TextEvidence")
@receiver(post_save, sender="phone.TextMessageEvidence")
@receiver(post_save, sender="phone.CallEvidenceAttachment")
def new_original(sender, instance, created, raw=False, **kwargs):
    if raw or not created or not settings.PANDECT_LOCAL_EMBEDDING.get("url"):
        return
    from .indexing import SEED_BATCH, add_sources

    source = {
        "textevidence": "text",
        "textmessageevidence": "message",
        "callevidenceattachment": "attachment",
    }[sender._meta.model_name]
    if source == "attachment" and not instance.transcript:
        return
    buffer = _batch.get()
    if source == "message" and buffer is not None:
        buffer.append(instance.pk)
        if len(buffer) >= SEED_BATCH:
            add_sources(instance.case_id, source, buffer)
            buffer.clear()
    else:
        add_sources(instance.case_id, source, [instance.pk])

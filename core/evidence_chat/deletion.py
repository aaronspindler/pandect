"""Collect a chat cascade without loading its source manifest into Python."""

from collections import Counter

from django.db import router
from django.db.models.deletion import (
    CASCADE,
    Collector,
    get_candidate_relations_to_delete,
)

from core.models import ChatMatch, ChatSource


class ChatDeletionCollector(Collector):
    def can_fast_delete(self, objs, from_field=None):
        if objs is ChatSource and from_field is ChatSource._meta.get_field("turn"):
            # The match query below covers the source's one dependent relation.
            # Use normal collection if the model gains other deletion behavior.
            match_source = ChatMatch._meta.get_field("source")
            return (
                from_field.remote_field.on_delete is CASCADE
                and match_source.remote_field.on_delete is CASCADE
                and not self._has_signal_listeners(ChatSource)
                and not ChatSource._meta.parents
                and not ChatSource._meta.private_fields
                and [
                    relation.field
                    for relation in get_candidate_relations_to_delete(ChatSource._meta)
                ]
                == [match_source]
                and super().can_fast_delete(
                    ChatMatch, from_field=ChatMatch._meta.get_field("turn")
                )
            )
        return super().can_fast_delete(objs, from_field=from_field)

    def related_objects(self, related_model, related_fields, objs):
        rows = super().related_objects(related_model, related_fields, objs)
        if (
            related_model is ChatMatch
            and related_fields == [ChatMatch._meta.get_field("turn")]
            and self.can_fast_delete(
                ChatSource, from_field=ChatSource._meta.get_field("turn")
            )
        ):
            # A match can be reached through either foreign key in the cascade.
            return rows | ChatMatch._base_manager.using(self.using).filter(
                source__turn__in=objs
            )
        return rows

    def record_counts(self):
        counts = Counter({model: len(rows) for model, rows in self.data.items()})
        for rows in self.fast_deletes:
            counts[rows.model] += rows.count()
        return dict(
            sorted(
                (
                    "chat matches"
                    if model is ChatMatch
                    else model._meta.verbose_name_plural,
                    count,
                )
                for model, count in counts.items()
                if count
            )
        )


def collect_chat_deletion(chat):
    collector = ChatDeletionCollector(
        using=router.db_for_write(type(chat), instance=chat)
    )
    collector.collect([chat])
    # Recursive replies can reach the same dependent rows. Count and delete them
    # once, with all matches removed before the source rows they reference.
    by_model = {}
    for rows in collector.fast_deletes:
        model = rows.model
        by_model[model] = by_model[model] | rows if model in by_model else rows
    collector.fast_deletes = sorted(
        by_model.values(), key=lambda rows: rows.model is ChatSource
    )
    return collector

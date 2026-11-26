"""Verified field queries and reusable message selections for Evidence Chat."""

from cases.models import CaseActivity
from core.models import ChatTurn
from people.models import Person
from phone import recorded_messages

from . import selections


def _revision(case, lock=False):
    rows = CaseActivity.objects.filter(case=case)
    if lock:
        rows = rows.select_for_update()
    return rows.values("evidence", "people").get()


def available(turn, *, lock=False):
    from .search_budget import timed_query

    saved = turn.plan.get("recorded_selection")
    if not saved:
        return True
    if saved.get("snapshot") and not selections.snapshot_owner_exists(turn, saved):
        return False
    if _revision(turn.case, lock) == saved["revision"]:
        return True
    if saved["parties"] != _party_labels(turn.case, saved):
        return False
    with timed_query():
        if saved.get("snapshot"):
            return selections.current_membership(turn, saved)
        return (
            recorded_messages.membership(recorded_messages.matching(turn.case, saved))
            == saved["membership"]
        )


def _party_labels(case, selection):
    ids = [
        selection.get("sender_id"),
        selection.get("recipient_id"),
        *selection.get("pair", ()),
    ]
    return {
        str(person.pk): str(person)
        for person in Person.objects.filter(case=case, pk__in=ids)
    }


def deletion_dependencies(case, message_ids):
    """Resolve only the message IDs in a reviewed permanent-deletion inventory."""
    keys = {str(pk) for pk in message_ids}
    turns = ChatTurn.objects.filter(
        case=case, chat__case=case, plan__has_key="recorded_selection"
    ).exclude(plan__recorded_selection__has_key="snapshot")
    affected = []
    for pk, selection in turns.values_list("pk", "plan__recorded_selection").iterator(
        chunk_size=200
    ):
        scope = selection["retention"]
        if "included" in scope:
            matched = not keys.isdisjoint(scope["included"])
        else:
            candidates = keys.difference(scope["excluded"])
            matched = (
                bool(candidates)
                and recorded_messages.matching(case, selection, archived=None)
                .filter(pk__in=candidates)
                .exists()
            )
        if matched:
            affected.append(pk)
    return affected

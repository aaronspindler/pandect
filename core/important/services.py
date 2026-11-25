"""Resolve and annotate exact, currently readable case items."""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

from cases.permissions import get_case
from core.analysis.important import finding_target
from core.evidence.queries import get_evidence
from core.evidence.types import KIND_LABELS
from core.evidence_chat.services import reply_available
from core.models import AnalysisResult, ImportantItem, ImportantNote
from core.models.chat import ChatTurn
from phone.presentation import message_labels

RESULT_RELATION = "finding_result__run__previous_run"


class InvalidMarkAction(ValueError):
    pass


def evidence_target(case, kind, evidence, *, title=None):
    return {
        "title": title or evidence.title,
        "label": KIND_LABELS[kind],
        "url": reverse("core:evidence:detail", args=[case.pk, kind, evidence.pk]),
    }


def _item_target(case, item):
    if item.chat_turn_id:
        return reply_target(case, item.chat_turn)
    if item.finding_result_id:
        return finding_target(case, item.finding_result, item.item_index)
    evidence = get_evidence(case, item.evidence_kind, item.evidence_id)
    if evidence.is_archived:
        raise Http404("Restore this evidence to open its notes.")
    title = None
    if item.evidence_kind == "text-message":
        title = message_labels(case, [evidence.pk]).get(evidence.pk)
    return evidence_target(case, item.evidence_kind, evidence, title=title)


def reply_target(case, turn):
    if (
        turn.case_id != case.pk
        or turn.chat.case_id != case.pk
        or not reply_available(turn)
    ):
        raise Http404("Reply unavailable.")
    return {
        "title": turn.question,
        "label": "Evidence chat reply",
        "url": reverse("core:chat:reply", args=[case.pk, turn.chat_id, turn.pk]),
    }


def _set_mark(actor, case, action, **target):
    if action == "mark":
        item, _ = ImportantItem.objects.get_or_create(case=case, **target)
        if item.marked_at is None:
            item.marked_at = timezone.now()
            item.marked_by = actor
            item.save(update_fields=("marked_at", "marked_by"))
    elif action == "unmark":
        ImportantItem.objects.filter(case=case, **target).update(
            marked_at=None, marked_by=None
        )
    else:
        raise InvalidMarkAction


@transaction.atomic
def set_evidence_mark(actor, case_id, kind, evidence_id, *, action):
    case = get_case(actor, case_id, edit=True, lock=True)
    evidence = get_evidence(case, kind, evidence_id, lock=True)
    if evidence.is_archived:
        raise Http404("Restore this evidence before changing its mark.")
    _set_mark(actor, case, action, evidence_kind=kind, evidence_id=evidence.pk)
    return reverse("core:evidence:detail", args=[case.pk, kind, evidence.pk])


@transaction.atomic
def set_finding_mark(actor, case_id, run_id, item_index, *, action):
    case = get_case(actor, case_id, edit=True, lock=True)
    result = get_object_or_404(
        AnalysisResult.objects.select_related("run"),
        case=case,
        run__case=case,
        run_id=run_id,
    )
    target = finding_target(case, result, item_index)
    _set_mark(actor, case, action, finding_result=result, item_index=item_index)
    return target["url"]


@transaction.atomic
def set_reply_mark(actor, case_id, turn_id, *, action):
    case = get_case(actor, case_id, edit=True, lock=True)
    turn = get_object_or_404(
        ChatTurn.objects.select_related("chat"), pk=turn_id, case=case, chat__case=case
    )
    target = reply_target(case, turn)
    _set_mark(actor, case, action, chat_turn=turn)
    return target["url"]


def read_note_item(case, item_id):
    item = get_object_or_404(
        ImportantItem.objects.select_related("chat_turn__chat", RESULT_RELATION),
        case=case,
        pk=item_id,
    )
    return item, _item_target(case, item)


@transaction.atomic
def submit_note(actor, case_id, item_id, form):
    case = get_case(actor, case_id, edit=True, lock=True)
    item, target = read_note_item(case, item_id)
    if item.marked_at is None:
        raise PermissionDenied("Mark this item important to add a note.")
    note_count = None
    if form.is_valid():
        ImportantNote.objects.create(
            item=item, author=actor, body=form.cleaned_data["body"]
        )
        note_count = item.notes.count()
    return case, item, target, note_count

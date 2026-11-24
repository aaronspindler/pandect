from django.core import signing
from django.urls import reverse

from core.models import ImportantItem

from .navigation import list_url, return_params, with_collection_return, with_return

RETURN_SALT = "core.important.reader"


def reader_context(
    request,
    case,
    *,
    source_url,
    evidence_kind=None,
    evidence_id=None,
    finding_result=None,
    item_index=None,
    chat_turn=None,
):
    if chat_turn is not None:
        target = {"chat_turn": chat_turn}
        action_url = reverse("core:important:mark_reply", args=[case.pk, chat_turn.pk])
    elif finding_result is None:
        target = {"evidence_kind": evidence_kind, "evidence_id": evidence_id}
        action_url = reverse(
            "core:important:mark_evidence", args=[case.pk, evidence_kind, evidence_id]
        )
    else:
        target = {"finding_result": finding_result, "item_index": item_index}
        action_url = reverse(
            "core:important:mark_finding",
            args=[case.pk, finding_result.run_id, item_index],
        )
    item = ImportantItem.objects.filter(case=case, **target).first()
    marked = item is not None and item.marked_at is not None
    params = return_params(request.GET)
    back_url = ""
    if item and params.get("important_item") == str(item.pk):
        back_url = list_url(case, page=params["important_page"], item_id=item.pk)
        source_url = with_return(source_url, params)
    notes_url = ""
    if item and (marked or item.notes.exists()):
        notes_path = reverse("core:important:notes", args=[case.pk, item.pk])
        notes_params = {"reader_return": reader_return_token(notes_path, source_url)}
        if back_url:
            notes_params["important_page"] = params["important_page"]
        notes_url = with_return(notes_path, notes_params)
    return {
        "important_marked": marked,
        "important_action_url": action_url,
        "important_reader_return": reader_return_token(action_url, source_url),
        "important_live_collection": evidence_kind is not None,
        "important_notes_url": notes_url,
        "important_back_url": back_url,
    }


def reader_return_token(action_path, source_url):
    return signing.dumps([action_path, source_url], salt=RETURN_SALT)


def reader_return_url(request, token, *, evidence=False):
    try:
        action_path, reader_url = signing.loads(token, salt=RETURN_SALT)
    except signing.BadSignature:
        return ""
    if action_path != request.path:
        return ""
    if evidence and "return_collection" in request.GET:
        return with_collection_return(reader_url, request.GET["return_collection"])
    return reader_url

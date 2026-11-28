"""Case authorization, owner consent and source verification for hosted retrieval."""

from uuid import UUID

from django.utils import timezone

from cases.permissions import get_case_for_verified_user
from core.models import HostedSearchIndex, HostedSearchItem

from . import cloudflare, material
from .cloudflare import HostedSearchError
from .lifecycle import case_setting_changed  # noqa: F401

NOTICES = {
    "search_disabled": "The case owner can enable external AI in Case settings.",
    "search_configuration": "Review the hosted search destination in Case settings.",
    "search_preparing": "Cloudflare is indexing this case. More passages may follow.",
    "search_unavailable": "Cloudflare AI Search is unavailable. Try again later.",
    "search_rate_limit": "Cloudflare AI Search is busy. Try again shortly.",
    "search_instance_private": "The private search instance settings need review.",
    "search_response_invalid": "Cloudflare returned an unreadable search response.",
    "search_item_failed": "Cloudflare could not index some passages. See Processing.",
}


def authorize(case, index):
    if not case.external_ai_enabled:
        raise HostedSearchError("search_disabled")
    approved = (case.external_analysis_approval or {}).get("hosted_search")
    if (
        case.external_ai_revision != index.revision
        or index.retired
        or case.pk != index.case_key
        or approved != {"account": index.account, "namespace": index.namespace}
        or approved != cloudflare.destination()
    ):
        raise HostedSearchError("search_configuration")


def hits(
    case,
    actor,
    query,
    *,
    through=None,
    limit=8,
    filters=None,
    records=False,
    authorize_transfer=None,
    deadline=None,
    omit_record_notes=False,
):
    query = query.strip()
    if not query or len(query) > 2000 or not 1 <= limit <= 50:
        return []
    if deadline is not None:
        cloudflare.check_deadline(deadline)
    through = through or timezone.now()
    case = get_case_for_verified_user(actor.pk, case.pk)
    if not case.external_ai_enabled:
        raise HostedSearchError("search_disabled")
    index = HostedSearchIndex.objects.filter(
        case_key=case.pk, revision=case.external_ai_revision, retired=False
    ).first()
    if index is None:
        raise HostedSearchError("search_preparing")
    authorize(case, index)
    if (
        index.status != "ready"
        and not HostedSearchItem.objects.filter(
            source__index=index, status__in=("completed", "check")
        ).exists()
    ):
        raise HostedSearchError(index.error_code or "search_preparing")
    filters = {k: v for k, v in (filters or {}).items() if v}
    remote_filters = {"pub": {"$lte": through.timestamp()}}
    if start := filters.get("start"):
        remote_filters["to_at"] = {"$gte": start.timestamp()}
    if end := filters.get("end"):
        remote_filters["from_at"] = {"$lt": end.timestamp()}
    if kind := filters.get("record_type"):
        remote_filters["surface" if kind in ("finding", "report") else "kind"] = kind
    if not records and "surface" not in remote_filters:
        remote_filters["surface"] = {"$nin": ["record", "social"]}

    def authorize_query():
        authorize(get_case_for_verified_user(actor.pk, case.pk), index)
        if authorize_transfer:
            authorize_transfer()

    authorize_query()
    reply = cloudflare.request(
        index,
        "POST",
        "/search",
        authorize_transfer=authorize_query,
        **({"deadline": deadline} if deadline is not None else {}),
        payload={
            "query": query,
            "ai_search_options": {
                "cache": {"enabled": False},
                "retrieval": {
                    "filters": remote_filters,
                    "max_num_results": min(50, limit * 2),
                    "retrieval_type": "hybrid",
                    "match_threshold": cloudflare.MATCH_THRESHOLD,
                    "keyword_match_mode": index.keyword_match_mode,
                },
                "query_rewrite": {"enabled": False},
                "reranking": {"enabled": False},
            },
        },
    )
    if authorize_transfer:
        authorize_transfer()
    if not isinstance(reply, dict) or not isinstance(reply.get("chunks"), list):
        raise HostedSearchError("search_response_invalid")
    case = get_case_for_verified_user(actor.pk, case.pk)
    authorize(case, index)
    candidates = []
    for hit in reply["chunks"][:50]:
        if not isinstance(hit, dict) or not isinstance(hit.get("item"), dict):
            continue
        key, text = hit["item"].get("key"), hit.get("text")
        if (
            not isinstance(key, str)
            or not key.endswith(".txt")
            or not isinstance(text, str)
            or not text.strip()
        ):
            continue
        try:
            item_id = UUID(hex=key[:-4])
        except ValueError:
            continue
        candidates.append((item_id, key, text))

    items = (
        HostedSearchItem.objects.select_related("source")
        .filter(
            source__index=index,
            available_at__lte=through,
            status__in=("completed", "check"),
        )
        .in_bulk({item_id for item_id, _, _ in candidates})
    )
    results = []
    seen = set()
    materials = {}
    note_free_records = {}
    for item_id, key, text in candidates:
        if deadline is not None:
            cloudflare.check_deadline(deadline)
        item = items.get(item_id)
        if (
            item is None
            or item.filename != key
            or (item.source.surface in ("record", "social") and not records)
        ):
            continue
        if item_id not in materials:
            materials[item_id] = material.read(
                case, item.source, item.start, through=through, filters=filters
            )
        found = materials[item_id]
        if (
            found is None
            or found.locator["version"] != item.version
            or material.corpus.digest(found.text) != item.digest
        ):
            continue
        offset = found.text.find(text)
        if offset < 0 or found.text.find(text, offset + 1) >= 0:
            continue
        if omit_record_notes and item.source.surface == "record":
            if item_id not in note_free_records:
                note_free_records[item_id] = material.read(
                    case,
                    item.source,
                    item.start,
                    through=through,
                    filters=filters,
                    omit_record_notes=True,
                )
            if (found := note_free_records[item_id]) is None:
                continue
            excerpt = found.text[offset : offset + len(text)]
            if not excerpt.strip() or not text.startswith(excerpt):
                continue
            text = excerpt
        start = item.start + offset
        identity = (found.locator["key"], start)
        if identity not in seen:
            seen.add(identity)
            results.append((found, start, text))
            if len(results) >= limit:
                break
    authorize(get_case_for_verified_user(actor.pk, case.pk), index)
    if deadline is not None:
        cloudflare.check_deadline(deadline)
    return results


def search(
    case,
    actor,
    query,
    *,
    through=None,
    limit=8,
    filters=None,
    authorize_transfer=None,
    omit_record_notes=False,
):
    return [
        (found.locator, start)
        for found, start, _ in hits(
            case,
            actor,
            query,
            through=through,
            limit=limit,
            filters=filters,
            authorize_transfer=authorize_transfer,
            records=True,
            omit_record_notes=omit_record_notes,
        )
    ]

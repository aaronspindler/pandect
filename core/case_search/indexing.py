"""Case processing status and publication hooks for the Cloudflare search outbox."""

from itertools import batched

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from cases.permissions import get_case, require_verified_email
from core.models import HostedSearchIndex, HostedSearchItem, HostedSearchSource

from .hosted import NOTICES, authorize
from .lifecycle import changed, enqueue, reconcile
from .material import source_count, surfaces


def add_sources(case_id, source, ids):
    # Bulk message imports bypass post_save.
    changed(case_id, "original", "text-message" if source == "message" else source, ids)


def evidence_archive_changed(case, actor, kind, item_id, *, archived):
    # A source can support extractions and findings with multiple dependencies.
    reconcile(case.pk)


def result_published(result):
    if result.run.operation in ("evidence_chat", "narrative_relationships"):
        return
    if not HostedSearchIndex.objects.filter(
        case_key=result.case_id, retired=False
    ).exists():
        return
    from core.models import AnalysisResult

    # A newer successful result can supersede an older version of this analysis.
    related = AnalysisResult.objects.filter(
        case_id=result.case_id,
        run__case_id=result.case_id,
        run__equivalence_key=result.run.equivalence_key,
    )
    changed(result.case_id, "report", "", [result.pk])
    for surface, relation in (("finding", "findings"), ("passage", "passages")):
        ids = related.exclude(**{relation + "__isnull": True}).values_list(
            relation + "__pk", flat=True
        )
        for chunk in batched(ids.iterator(chunk_size=500), 500, strict=False):
            changed(result.case_id, surface, "", chunk)


def search_index_status(case, *, count_passages=True):
    index = None
    if case.external_ai_enabled:
        index = HostedSearchIndex.objects.filter(
            case_key=case.pk, retired=False, revision=case.external_ai_revision
        ).first()
    status = (
        "disabled"
        if not case.external_ai_enabled
        else (index.status if index else "unprepared")
    )
    reason = (index.error_code if index else "") or ""
    counts = {"documents": 0, "passages": 0}
    sources_in_scope = None
    if index:
        items = HostedSearchItem.objects.filter(source__index=index)
        indexed = Q(status__in=("completed", "check"))
        if count_passages:
            sources_in_scope = source_count(case)
            # These are local passage receipts. Discovery may still add to them.
            counts = items.exclude(status="delete").aggregate(
                documents=Count("source_id", filter=indexed, distinct=True),
                passages=Count("pk", filter=indexed),
                awaiting_upload=Count("pk", filter=Q(status__in=("queued", "recover"))),
                indexing=Count("pk", filter=Q(status="poll")),
                rechecking=Count("pk", filter=Q(status="check")),
                failed=Count("pk", filter=Q(status="error")),
                checked_at=Max("checked_at"),
            )
            counts["preparing"] = status == "queued" and (
                index.phase < len(surfaces())
                or HostedSearchSource.objects.filter(index=index, dirty=True).exists()
            )
        else:
            counts["passages"] = int(items.filter(indexed).exists())
    return {
        "status": status,
        "label": {
            "disabled": "External AI is disabled for this case.",
            "unprepared": "The private search instance is awaiting preparation.",
            "queued": "Cloudflare search indexing is in progress.",
            "ready": "Cloudflare search is ready.",
            "failed": "Cloudflare search indexing needs attention.",
        }[status],
        "reason": "Cloudflare could not index some passages."
        if reason == "search_item_failed"
        else NOTICES.get(
            reason, "Cloudflare search indexing was interrupted." if reason else ""
        ),
        "provisioned": bool(index and index.provisioned),
        "sources_in_scope": sources_in_scope,
        **counts,
        "can_retry": status == "failed",
        "token": index.token if index else None,
    }


@transaction.atomic
def prepare_case_search(case, actor, *, retry=False, interrupted_token=None):
    case = get_case(actor, case.pk, edit=True, lock=True)
    require_verified_email(actor)
    index = (
        HostedSearchIndex.objects.select_for_update()
        .filter(case_key=case.pk, retired=False, revision=case.external_ai_revision)
        .first()
    )
    if not index or not case.external_ai_enabled:
        return False
    authorize(case, index)
    if interrupted_token is not None and (
        index.token != interrupted_token or index.status != "queued"
    ):
        return False
    if index.status == "queued" and not interrupted_token:
        return False
    if retry or interrupted_token or index.status == "ready":
        HostedSearchItem.objects.filter(source__index=index, status="error").update(
            status="queued", available_at=timezone.now()
        )
        HostedSearchItem.objects.filter(source__index=index, status="completed").update(
            status="check"
        )
        if interrupted_token:
            HostedSearchItem.objects.filter(
                source__index=index, status="queued"
            ).update(status="recover")
        HostedSearchSource.objects.filter(index=index).update(dirty=True, offset=0)
        index.provisioned = False
        index.phase, index.cursor, index.failures, index.error_code = 0, None, 0, ""
        enqueue(index)
        return True
    return False

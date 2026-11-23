"""Recoverable private passage preparation on the existing database queue."""

import hashlib
from datetime import timedelta
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.tasks import task
from django.utils import timezone

from cases.permissions import get_case
from core.analysis.errors import AnalysisError, error_message
from core.case_search.approval import search_approval_current
from core.case_search.semantic_model import (
    BATCH_SIZE,
    CHUNK_BYTES,
    embed,
    execution,
    is_external,
    next_chunk,
    profile,
)
from core.case_search.semantic_model import (
    identity as embedding_identity,
)
from core.case_search.semantic_sources import load_windows, source_queryset
from core.models import CaseSearchIndex, SearchDocument, SearchPassage
from core.models.search import SOURCE_FIELDS

SEED_BATCH = 500
RETRY_DELAYS = (15, 60)
TRANSIENT_ERRORS = {
    "gateway_rate_limit",
    "gateway_unavailable",
    "local_ai_busy",
    "local_ai_unavailable",
}
LABELS = {
    "disabled": "Meaning search is unavailable in this installation.",
    "awaiting_approval": "The case owner must approve meaning search in Case settings.",
    "unprepared": "The meaning search index has not been prepared yet.",
    "changed": "Meaning search settings changed. Rebuild the index in Processing.",
    "queued": "Meaning search indexing is queued.",
    "running": "Meaning search indexing is running.",
    "failed": "Meaning search indexing stopped.",
    "ready": "Meaning search is ready.",
}


def search_index_status(case, *, count_passages=True):
    identity = profile()
    status = "disabled"
    reason = ""
    token = None
    passages = documents = 0
    if identity and is_external() and not search_approval_current(case):
        status = "awaiting_approval"
    elif identity:
        index = CaseSearchIndex.objects.filter(case=case).first()
        status = "unprepared"
        if index:
            status = index.status if index.profile == identity else "changed"
            token = index.token
            if status != "changed":
                indexed = SearchPassage.objects.filter(
                    case=case,
                    profile=identity,
                    document__case=case,
                    document__profile=identity,
                )
                if count_passages:
                    documents = SearchDocument.objects.filter(
                        case=case, profile=identity, complete=True
                    ).count()
                    passages = indexed.count()
                else:
                    passages = int(indexed.filter(document__complete=True).exists())
                if index.error_code:
                    if status == "queued":
                        reason = (
                            "The embedding service is busy or unavailable. "
                            "An automatic retry is scheduled."
                        )
                    elif index.error_code == "paused_by_operator":
                        reason = (
                            "An operator paused indexing. Resume to continue "
                            "from the saved position."
                        )
                    else:
                        reason = error_message(index.error_code)
    return {
        "status": status,
        "label": LABELS[status],
        "token": token,
        "reason": reason,
        "passages": passages,
        "documents": documents,
        "can_retry": status in {"failed", "changed", "unprepared"},
    }


def _enqueue(index, *, delay=0):
    index.token = uuid4()
    index.status = "queued"
    index.save()
    delivery = index_case_passages
    if delay:
        delivery = delivery.using(run_after=timezone.now() + timedelta(seconds=delay))
    delivery.enqueue(str(index.pk), str(index.token))


@transaction.atomic
def prepare_case_search(case, user, *, retry=False, interrupted_token=None):
    case = get_case(user, case.pk, edit=True, lock=True)
    if not EmailAddress.objects.filter(user=user, verified=True).exists():
        raise PermissionDenied
    identity = profile()
    if not identity or (is_external() and not search_approval_current(case)):
        return False
    index = CaseSearchIndex.objects.select_for_update().filter(case=case).first()
    if interrupted_token is not None and (
        not index
        or index.token != interrupted_token
        or index.status not in ("queued", "running")
    ):
        return False
    config = embedding_identity(execution())
    if is_external():
        config["approval_revision"] = case.search_embedding_approval["revision"]
    if index:
        changed = index.profile != identity or index.execution != config
        if (
            not changed
            and interrupted_token is None
            and not (retry and index.status == "failed")
        ):
            return False
        if changed:
            index.source_type_index = 0
            index.source_cursor = None
    else:
        index = CaseSearchIndex(case=case)
    index.requested_by = user
    index.profile = identity
    index.execution = config
    index.error_code = ""
    index.failures = 0
    _enqueue(index)
    return True


def _bulk_sources(case, source, ids):
    SearchDocument.objects.bulk_create(
        [SearchDocument(case=case, **{f"{source}_id": pk}) for pk in ids],
        ignore_conflicts=True,
        batch_size=SEED_BATCH,
    )


@transaction.atomic
def add_sources(case_id, source, ids):
    """Called in the publishing transaction; registering work never sends evidence."""
    identity = profile()
    if not identity:
        return
    from cases.models import Case

    case = Case.objects.select_for_update().get(pk=case_id)
    if is_external() and not search_approval_current(case):
        return
    _bulk_sources(case, source, ids)
    index = CaseSearchIndex.objects.select_for_update().filter(case=case).first()
    if index is None:
        if EmailAddress.objects.filter(
            user=case.owner, user__is_active=True, verified=True
        ).exists():
            prepare_case_search(case, case.owner)
    elif index.status == "ready" and index.profile == identity:
        index.failures = 0
        _enqueue(index)


def result_published(result):
    if result.run.operation == "sighting_group" or not profile():
        return
    from itertools import batched

    for source, rows in (("finding", result.findings), ("extraction", result.passages)):
        for ids in batched(
            rows.values_list("pk", flat=True).iterator(chunk_size=SEED_BATCH),
            SEED_BATCH,
            strict=False,
        ):
            add_sources(result.case_id, source, ids)


def authorize(user_id, case_id, *, edit=False, lock=False):
    actor = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if (
        actor is None
        or not EmailAddress.objects.filter(user_id=user_id, verified=True).exists()
    ):
        raise PermissionDenied
    return get_case(actor, case_id, edit=edit, lock=lock)


class Superseded(Exception):
    pass


def _authorize(index, *, lock=False):
    case = authorize(index.requested_by_id, index.case_id, edit=True, lock=lock)
    if index.profile != profile():
        raise AnalysisError("execution_changed")
    if is_external():
        revision = index.execution.get("approval_revision")
        if not revision or not search_approval_current(case, revision=revision):
            raise AnalysisError("embedding_approval_required")
    if not CaseSearchIndex.objects.filter(
        pk=index.pk, token=index.token, status="running"
    ).exists():
        raise Superseded
    return case


def _seed(index, case):
    source = SOURCE_FIELDS[index.source_type_index]
    queryset = source_queryset(case, source)
    if index.source_cursor:
        queryset = queryset.filter(pk__gt=index.source_cursor)
    ids = list(queryset.order_by("pk").values_list("pk", flat=True)[:SEED_BATCH])
    _bulk_sources(case, source, ids)
    # Allow rebuilding large cases in small transactions while the stale
    # profile is already ineligible for retrieval.
    SearchDocument.objects.filter(case=case, **{f"{source}_id__in": ids}).exclude(
        profile=index.profile
    ).update(complete=False)
    if len(ids) < SEED_BATCH:
        index.source_type_index += 1
        index.source_cursor = None
    else:
        index.source_cursor = ids[-1]
    _enqueue(index)


def _batch(index, docs, windows):
    """Keep one request and one atomic write per bounded batch of passages."""
    pending = []
    progress = []
    for doc in docs:
        start = doc.offset if doc.profile == index.profile else 0
        text = windows.get(doc.source(), ("", {}))[0]
        pos = 0
        while pos < len(text) and len(pending) < BATCH_SIZE:
            chunk = next_chunk(text[pos:])
            if chunk.strip():
                pending.append((doc, start + pos, chunk))
            pos += len(chunk)
        finished = pos == len(text) and len(text) < WINDOW
        progress.append((doc, start + pos, finished))
        if len(pending) == BATCH_SIZE:
            break
    return pending, progress


WINDOW = BATCH_SIZE * CHUNK_BYTES + 1


@task
def index_case_passages(index_id, token):
    if not CaseSearchIndex.objects.filter(
        pk=index_id, token=token, status="queued"
    ).update(status="running"):
        return "unavailable"
    index = CaseSearchIndex.objects.filter(
        pk=index_id, token=token, status="running"
    ).first()
    if index is None:
        return "superseded"
    try:
        with transaction.atomic():
            case = _authorize(index, lock=True)
            if index.source_type_index < len(SOURCE_FIELDS):
                _seed(index, case)
                return "queued"
            docs = list(
                SearchDocument.objects.filter(case=case)
                .filter(complete=False)
                .order_by("pk")[:BATCH_SIZE]
            )
            if not docs:
                index.status = "ready"
                index.save(update_fields=("status",))
                return "ready"
        requests = [(d, d.offset if d.profile == index.profile else 0) for d in docs]
        windows = load_windows(case, requests, WINDOW)
        pending, progress = _batch(index, docs, windows)
        vectors = (
            embed(
                [text for _, _, text in pending],
                lambda: _authorize(index),
                case=case,
                purpose="index",
            )
            if pending
            else []
        )
        with transaction.atomic():
            _authorize(index, lock=True)
            # An authorized deletion can remove a document during inference.
            # Only previously inspected, still-current source windows may publish.
            current = SearchDocument.objects.filter(case=case).in_bulk(
                [d.pk for d, _, _ in progress]
            )
            live = load_windows(
                case, [(d, offset) for d, offset in requests if d.pk in current], WINDOW
            )
            valid = {
                d.pk
                for d, _, _ in progress
                if d.pk in current and windows.get(d.source()) == live.get(d.source())
            }
            restarting = [
                doc.pk
                for doc, _, _ in progress
                if doc.pk in valid and doc.profile and doc.profile != index.profile
            ]
            if restarting:
                # A previous attempt may have partially built this same profile.
                SearchPassage.objects.filter(
                    document_id__in=restarting, profile=index.profile
                ).delete()
            for doc, offset, complete in progress:
                if doc.pk in valid:
                    doc.profile, doc.offset, doc.complete = (
                        index.profile,
                        offset,
                        complete,
                    )
            SearchDocument.objects.bulk_update(
                [d for d, _, _ in progress if d.pk in valid],
                ["profile", "offset", "complete"],
            )
            SearchPassage.objects.bulk_create(
                [
                    SearchPassage(
                        case=case,
                        document=doc,
                        profile=index.profile,
                        start=start,
                        length=len(text),
                        digest=hashlib.sha256(text.encode()).hexdigest(),
                        embedding=vector,
                    )
                    for (doc, start, text), vector in zip(pending, vectors, strict=True)
                    if doc.pk in valid
                ]
            )
            finished = [
                d.pk for d, _, complete in progress if complete and d.pk in valid
            ]
            if finished:
                SearchPassage.objects.filter(document_id__in=finished).exclude(
                    profile=index.profile
                ).delete()
            index.error_code = ""
            index.failures = 0
            _enqueue(index)
        return "queued"
    except Superseded:
        return "superseded"
    except PermissionDenied, Http404:
        code = "access_removed"
    except AnalysisError as error:
        code = error.code
    except Exception:
        code = "processing_failed"
    with transaction.atomic():
        current = (
            CaseSearchIndex.objects.select_for_update()
            .filter(pk=index.pk, token=token, status="running")
            .first()
        )
        if not current:
            return "superseded"
        current.failures += 1
        current.error_code = code
        if code in TRANSIENT_ERRORS and current.failures <= len(RETRY_DELAYS):
            _enqueue(current, delay=RETRY_DELAYS[current.failures - 1])
            return "queued"
        current.status = "failed"
        current.save(update_fields=("status", "failures", "error_code"))
    return "failed"

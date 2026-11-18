"""Recoverable private passage preparation on the existing database queue."""

import hashlib
from uuid import uuid4

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.tasks import task

from cases.permissions import get_case
from core.analysis.errors import AnalysisError
from core.case_search.semantic_model import (
    BATCH_SIZE,
    CHUNK_BYTES,
    embed,
    execution,
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
LABELS = {
    "disabled": "Meaning search requires a configured private embedding model.",
    "unprepared": "The meaning search index has not been prepared yet.",
    "changed": "The meaning search model changed. Rebuild the index in Processing.",
    "queued": "Meaning search is indexing this case. Some passages may be missing.",
    "running": "Meaning search is indexing this case. Some passages may be missing.",
    "failed": "Meaning search indexing stopped. Resume it from Processing.",
    "ready": "Meaning search is ready.",
}


def search_index_status(case, *, count_passages=True):
    identity = profile()
    index = CaseSearchIndex.objects.filter(case=case).first() if identity else None
    status = "disabled" if not identity else "unprepared"
    passages = 0
    if index:
        status = index.status if index.profile == identity else "changed"
        if status != "changed":
            published = SearchPassage.objects.filter(
                case=case,
                document__case=case,
                document__profile=identity,
                document__complete=True,
            )
            passages = published.count() if count_passages else int(published.exists())
    return {
        "status": status,
        "label": LABELS[status],
        "passages": passages,
        "can_retry": status in {"failed", "changed", "unprepared", "queued", "running"},
    }


def _enqueue(index):
    index.token = uuid4()
    index.status = "queued"
    index.save()
    index_case_passages.enqueue(str(index.pk), str(index.token))


@transaction.atomic
def prepare_case_search(case, user, *, retry=False):
    case = get_case(user, case.pk, edit=True, lock=True)
    if not EmailAddress.objects.filter(user=user, verified=True).exists():
        raise PermissionDenied
    identity = profile()
    if not identity:
        return False
    index, created = CaseSearchIndex.objects.select_for_update().get_or_create(
        case=case, defaults={"profile": identity, "requested_by": user}
    )
    changed = index.profile != identity
    if (
        not created
        and not changed
        and not (retry and index.status in ("queued", "running", "failed"))
    ):
        return False
    if changed:
        index.source_type_index = 0
        index.source_cursor = None
    index.requested_by = user
    index.profile = identity
    index.execution = embedding_identity(execution())
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
    if not profile():
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
            embed([text for _, _, text in pending], lambda: _authorize(index))
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
            previous_profiles = [
                doc.pk
                for doc, _, _ in progress
                if doc.pk in valid and doc.profile and doc.profile != index.profile
            ]
            if previous_profiles:
                SearchPassage.objects.filter(document_id__in=previous_profiles).delete()
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
                        start=start,
                        length=len(text),
                        digest=hashlib.sha256(text.encode()).hexdigest(),
                        embedding=vector,
                    )
                    for (doc, start, text), vector in zip(pending, vectors, strict=True)
                    if doc.pk in valid
                ]
            )
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
        if current.failures < 3 and code in {"local_ai_busy", "local_ai_unavailable"}:
            _enqueue(current)
            return "queued"
        current.status = "failed"
        current.save(update_fields=("status", "failures", "error_code"))
    return "failed"

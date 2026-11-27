"""Transactional delivery of private search instances and versioned passage windows."""

import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from math import ceil
from urllib.parse import urlencode
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import connections, transaction
from django.db.models import F, Q
from django.http import Http404
from django.tasks import task
from django.utils import timezone

from cases.models import Case, CaseActivity
from cases.permissions import get_case_for_verified_user
from core.models import HostedSearchIndex, HostedSearchItem, HostedSearchSource

from . import cloudflare, material
from .cloudflare import HostedSearchError

PREPARE_LIMIT = 16
UPLOAD_LIMIT = 4
STATUS_LIMIT = 50
# Cloudflare accepts at most ten values in an item metadata $in filter.
STATUS_FILTER_LIMIT = 10
STATUS_FALLBACK_LIMIT = 2
POLL_INTERVAL = timedelta(seconds=30)
DELIVERY_ERRORS = (HostedSearchError, PermissionDenied, Http404)


def activity_changed(index):
    case_id = index.case_key
    transaction.on_commit(
        lambda: CaseActivity.objects.filter(case_id=case_id).update(work=F("work") + 1)
    )


def enqueue(index, *, run_after=None):
    index.token = uuid4()
    index.status = "queued"
    index.save()
    delivery = sync_index
    if run_after is not None and run_after > timezone.now():
        delivery = delivery.using(run_after=run_after)
    delivery.enqueue(str(index.pk), str(index.token))


def retire(index):
    index.retired = True
    index.failures = 0
    index.error_code = ""
    # Keep only the cleanup destination when the owner withdraws consent.
    HostedSearchSource.objects.filter(index=index).delete()
    enqueue(index)


@transaction.atomic
def case_setting_changed(case):
    live = HostedSearchIndex.objects.select_for_update().filter(
        case_key=case.pk, retired=False
    )
    approved = (case.external_analysis_approval or {}).get("hosted_search") or {}
    for old in live:
        if case.external_ai_enabled and approved == {
            "account": old.account,
            "namespace": old.namespace,
        }:
            if old.revision != case.external_ai_revision:
                old.revision = case.external_ai_revision
                if old.status == "queued":
                    enqueue(old)
                else:
                    old.token = uuid4()
                    old.save(update_fields=["revision", "token"])
            return
        retire(old)
    if case.external_ai_enabled:
        index, created = HostedSearchIndex.objects.get_or_create(
            case_key=case.pk,
            revision=case.external_ai_revision,
            defaults={
                "account": approved.get("account", ""),
                "namespace": approved.get("namespace", ""),
            },
        )
        if created:
            enqueue(index)


def active(index):
    from .hosted import authorize

    owner_id = (
        Case.objects.filter(pk=index.case_key)
        .values_list("owner_id", flat=True)
        .first()
    )
    if owner_id is None:
        raise HostedSearchError("search_disabled")
    case = get_case_for_verified_user(owner_id, index.case_key, edit=True)
    authorize(case, index)
    return case


def insert_sources(index, surface, kind, ids, *, refresh):
    rows = [
        HostedSearchSource(index=index, surface=surface, kind=kind, source_key=pk)
        for pk in ids
    ]
    if rows:
        HostedSearchSource.objects.bulk_create(
            rows,
            ignore_conflicts=not refresh,
            update_conflicts=refresh,
            update_fields=["dirty", "offset"] if refresh else None,
            unique_fields=["index", "surface", "kind", "source_key"]
            if refresh
            else None,
            batch_size=500,
        )


@transaction.atomic
def changed(case_id, surface, kind, ids):
    index = (
        HostedSearchIndex.objects.select_for_update()
        .filter(case_key=case_id, retired=False)
        .first()
    )
    if not index:
        return False
    insert_sources(index, surface, kind, ids, refresh=True)
    if index.status == "ready" or index.error_code == "search_item_failed":
        enqueue(index)
    return True


@transaction.atomic
def reconcile(case_id):
    index = (
        HostedSearchIndex.objects.select_for_update()
        .filter(case_key=case_id, retired=False)
        .first()
    )
    if index:
        index.phase, index.cursor = 0, None
        HostedSearchSource.objects.filter(index=index).update(dirty=True, offset=0)
        if index.status == "ready":
            enqueue(index)
        else:
            index.save(update_fields=["phase", "cursor"])


def prepare_window(case, source):
    items = HostedSearchItem.objects.filter(source=source)
    found = material.read(case, source, source.offset)
    dates = material.event_dates(case, source, found.locator) if found else None
    if dates is None:
        items.update(status="delete")
        source.dirty = False
    else:
        identity = {
            "version": found.locator["version"],
            "event_start": dates[0],
            "event_end": dates[1],
        }
        if source.offset == 0:
            items.exclude(**identity).update(status="delete")
        if found.text.strip():
            item, _ = items.get_or_create(
                source=source,
                **identity,
                start=source.offset,
                defaults={
                    "length": len(found.text),
                    "digest": material.corpus.digest(found.text),
                    "available_at": timezone.now(),
                },
            )
            if item.status == "delete":
                item.status = "queued"
                item.save(update_fields=["status"])
        source.offset += material.WINDOW
        source.dirty = source.offset < found.locator["length"]
    source.save(update_fields=["dirty", "offset"])


def prepare_passages(case, sources):
    pending = deque(sources.filter(dirty=True).order_by("id")[:PREPARE_LIMIT])
    if not pending:
        return False
    for _ in range(PREPARE_LIMIT):
        if not pending:
            break
        source = pending.popleft()
        prepare_window(case, source)
        if source.dirty:
            pending.append(source)
    return True


def receipt(item, value):
    if (
        not isinstance(value, dict)
        or value.get("key") != item.filename
        or not isinstance(value.get("id"), str)
    ):
        raise HostedSearchError("search_response_invalid")
    cloudflare.item_path(value["id"])
    status = value.get("status")
    if status not in {"queued", "running", "completed", "error", "skipped", "outdated"}:
        raise HostedSearchError("search_response_invalid")
    item.remote_id = value["id"]
    item.status = {
        "queued": "poll",
        "running": "poll",
        "skipped": "error",
        "outdated": "error",
    }.get(status, status)
    if item.status == "poll" and item.available_at < timezone.now() - timedelta(days=1):
        item.status = "error"
    if value.get("warnings"):
        item.status = "error"
    item.checked_at = timezone.now()
    item.save(update_fields=["remote_id", "status", "checked_at"])


def remote_item(index, item):
    listing = cloudflare.request(
        index, "GET", "/items?source=builtin&key=" + item.filename
    )
    if not isinstance(listing, list):
        raise HostedSearchError("search_response_invalid")
    for row in listing:
        if not isinstance(row, dict):
            raise HostedSearchError("search_response_invalid")
        if row.get("key") == item.filename:
            return row
    return None


def delete_passage(index, item):
    active(index)
    remote_id = item.remote_id
    if not remote_id:
        # A timed-out upload may have succeeded with a lost receipt.
        if row := remote_item(index, item):
            remote_id = row.get("id")
            if not isinstance(remote_id, str):
                raise HostedSearchError("search_response_invalid")
    if remote_id:
        active(index)
        cloudflare.request(
            index, "DELETE", cloudflare.item_path(remote_id), missing_ok=True
        )
    item.delete()


def upload_file(index, item):
    case = active(index)
    if item.status == "recover" or (index.failures and item.status == "queued"):
        row = remote_item(index, item)
        if row:
            receipt(item, row)
            return None
    found = material.read(case, item.source, item.start)
    if (
        not found
        or found.locator["version"] != item.version
        or material.corpus.digest(found.text) != item.digest
        or material.event_dates(case, item.source, found.locator)
        != (item.event_start, item.event_end)
    ):
        item.status = "delete"
        item.save(update_fields=["status"])
        item.source.dirty, item.source.offset = True, 0
        item.source.save(update_fields=["dirty", "offset"])
        return None
    metadata = {
        "pub": str(item.available_at.timestamp()),
        "from_at": str(item.event_start.timestamp()),
        "to_at": str(item.event_end.timestamp()),
        "kind": found.locator["kind"],
        "surface": item.source.surface,
    }
    return item.filename, found.text, metadata


def send_file(index, file, **kwargs):
    return cloudflare.request(
        index,
        "POST",
        "/items",
        file=file,
        authorize_transfer=lambda: active(index),
        **kwargs,
    )


def send_file_in_thread(index, file, usage):
    # The caller holds the index lock. Threads recheck the grant and do network
    # I/O, while the caller owns receipt writes and post-commit usage accounting.
    try:
        return send_file(index, file, usage_callback=usage.append)
    finally:
        connections.close_all()


def finish_upload(item, response):
    try:
        receipt(item, response())
    except DELIVERY_ERRORS as exc:
        if not getattr(exc, "retry_at", None):
            # Either peer may have reached Cloudflare before an error was observed.
            item.status = "recover"
            item.save(update_fields=["status"])
        return exc
    return None


def send_passages(index, pending):
    files = [(item, file) for item in pending if (file := upload_file(index, item))]
    if not files:
        return
    if len(files) == 1:
        item, file = files[0]
        if error := finish_upload(item, lambda: send_file(index, file)):
            raise error
        return
    errors = []
    with ThreadPoolExecutor(max_workers=UPLOAD_LIMIT) as pool:
        uploads = []
        for item, file in files:
            usage = []
            future = pool.submit(send_file_in_thread, index, file, usage)
            uploads.append((item, future, usage))
        for item, future, usage in uploads:
            if error := finish_upload(item, future.result):
                errors.append(error)
            for record in usage:
                transaction.on_commit(record)
    if errors:
        # Give an actual provider cooldown priority over a local deferral.
        error = next(
            (err for err in errors if getattr(err, "provider_throttled", False)), None
        )
        if not error:
            error = next(
                (err for err in errors if not getattr(err, "retry_at", None)), errors[0]
            )
        raise error


def list_passage_statuses(index, targets=()):
    params = {"source": "builtin", "per_page": STATUS_LIMIT}
    if targets:
        params["metadata_filter"] = json.dumps(
            {"filename": {"$in": [item.filename for item in targets]}},
            separators=(",", ":"),
        )
    else:
        params["sort_by"] = "modified_at"
    active(index)
    listing = cloudflare.request(index, "GET", "/items?" + urlencode(params))
    if not isinstance(listing, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str)
        for row in listing
    ):
        raise HostedSearchError("search_response_invalid")
    return {row["id"]: row for row in listing}


def reconcile_statuses(items, by_id):
    missing = []
    for item in items:
        if item.remote_id in by_id:
            receipt(item, by_id[item.remote_id])
        else:
            missing.append(item)
    return missing


def check_passages(index, due, neighbors):
    missing = due
    candidates = due + neighbors
    if len(candidates) > 1:
        targeted = len(candidates) <= STATUS_FILTER_LIMIT
        statuses = list_passage_statuses(index, candidates if targeted else ())
        missing = reconcile_statuses(due, statuses)
        reconcile_statuses(neighbors, statuses)
        if not targeted and len(missing) > STATUS_FALLBACK_LIMIT:
            # The newest page can omit older receipts that are still due.
            targets = missing[:STATUS_FILTER_LIMIT]
            statuses = list_passage_statuses(index, targets)
            missing = (
                reconcile_statuses(targets, statuses) + missing[STATUS_FILTER_LIMIT:]
            )
    # A list response cannot establish absence. Confirm due misses by ID.
    for item in missing[:STATUS_FALLBACK_LIMIT]:
        active(index)
        value = cloudflare.request(
            index, "GET", cloudflare.item_path(item.remote_id), missing_ok=True
        )
        if value is None:
            item.status, item.remote_id = "queued", ""
            item.save(update_fields=["status", "remote_id"])
        else:
            if not isinstance(value, dict) or value.get("id") != item.remote_id:
                raise HostedSearchError("search_response_invalid")
            receipt(item, value)


def discover_sources(index, case):
    surface, kind = material.surfaces()[index.phase]
    all_rows = material.rows(case, surface, kind)
    if index.cursor:
        all_rows = all_rows.filter(pk__gt=index.cursor)
    ids = list(all_rows.order_by("pk").values_list("pk", flat=True)[:500])
    insert_sources(index, surface, kind, ids, refresh=False)
    if len(ids) == 500:
        index.cursor = ids[-1]
    else:
        index.phase += 1
        index.cursor = None


def advance_passages(index, case):
    sources = HostedSearchSource.objects.filter(index=index)
    items = HostedSearchItem.objects.filter(source__index=index)
    if item := items.filter(status="delete").order_by("pk").first():
        delete_passage(index, item)
        return 0
    # When the oldest check is due, reconcile neighboring receipts in the same
    # request so recently submitted items do not each trigger their own poll.
    checks = items.filter(status__in=("poll", "check")).order_by("checked_at", "pk")
    due = checks.filter(
        Q(status="check") | Q(checked_at__lte=timezone.now() - POLL_INTERVAL)
    )
    if pending_checks := list(due[:STATUS_LIMIT]):
        neighbors = list(
            checks.exclude(pk__in=[item.pk for item in pending_checks])[
                : STATUS_LIMIT - len(pending_checks)
            ]
        )
        check_passages(index, pending_checks, neighbors)
        return 0

    uploads = (
        items.filter(status__in=("queued", "recover"))
        .select_related("source")
        .order_by("checked_at", "pk")
    )
    pending = list(uploads[:UPLOAD_LIMIT])
    if len(pending) < UPLOAD_LIMIT and prepare_passages(case, sources):
        # Commit newly staged receipt keys before any upload can reach Cloudflare.
        return 0
    if pending:
        send_passages(index, pending)
        return 0
    if index.phase < len(material.surfaces()):
        discover_sources(index, case)
        return 0
    if oldest := checks.first():
        return max(
            0,
            ceil((oldest.checked_at + POLL_INTERVAL - timezone.now()).total_seconds()),
        )
    return None


def step(index):
    if index.retired:
        cloudflare.request(index, "DELETE", missing_ok=True)
        if cloudflare.request(index, "GET", missing_ok=True) is not None:
            raise HostedSearchError("search_cleanup_pending")
        index.delete()
        return False
    case = active(index)
    delay = 0
    if not index.provisioned:
        cloudflare.ensure_instance(index, lambda: active(index))
        index.provisioned = True
    else:
        delay = advance_passages(index, case)
        if delay is None:
            index.status = (
                "failed"
                if HostedSearchItem.objects.filter(
                    source__index=index, status="error"
                ).exists()
                else "ready"
            )
            index.error_code = "search_item_failed" if index.status == "failed" else ""
            index.save()
            return True
    had_error = bool(index.error_code)
    index.failures = 0
    index.error_code = ""
    enqueue(
        index, run_after=timezone.now() + timedelta(seconds=delay) if delay else None
    )
    return delay == 0 or had_error


@task
def sync_index(index_id, token):
    # Serialize delivery with disable and deletion. A timeout can replay only the
    # same instance or immutable item key, including after a process crash.
    with transaction.atomic():
        index = (
            HostedSearchIndex.objects.select_for_update()
            .filter(pk=index_id, token=token)
            .first()
        )
        if index is None:
            return "superseded"
        try:
            progress_changed = step(index)
        except DELIVERY_ERRORS as exc:
            progress_changed = True
            index.error_code = getattr(exc, "code", "search_disabled")
            retry_at = getattr(exc, "retry_at", None)
            if retry_at and not exc.provider_throttled:
                # A peer upload can use up the wait before the batch finishes.
                enqueue(index, run_after=retry_at)
            else:
                index.failures += 1
                if index.failures <= 3:
                    backoff = timezone.now() + timedelta(
                        seconds=(15, 60, 240)[index.failures - 1]
                    )
                    enqueue(index, run_after=max(backoff, retry_at or backoff))
                else:
                    index.status = "failed"
                    index.save()
        if progress_changed and not index.retired:
            activity_changed(index)
        return index.status

"""Transactional delivery of private search instances and versioned passage windows."""

from datetime import timedelta
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404
from django.tasks import task
from django.utils import timezone

from cases.models import Case, CaseActivity
from cases.permissions import get_case_for_verified_user
from core.models import HostedSearchIndex, HostedSearchItem, HostedSearchSource

from . import cloudflare, material
from .cloudflare import HostedSearchError


def activity_changed(index):
    case_id = index.case_key
    transaction.on_commit(
        lambda: CaseActivity.objects.filter(case_id=case_id).update(work=F("work") + 1)
    )


def enqueue(index, delay=0):
    index.token = uuid4()
    index.status = "queued"
    index.save()
    delivery = sync_index
    if delay:
        delivery = delivery.using(run_after=timezone.now() + timedelta(seconds=delay))
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


def prepare_window(index, case, source):
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
            item, created = items.get_or_create(
                source=source,
                **identity,
                start=source.offset,
                defaults={
                    "length": len(found.text),
                    "digest": material.corpus.digest(found.text),
                    "available_at": timezone.now(),
                },
            )
            if created:
                activity_changed(index)
            if item.status == "delete":
                item.status = "queued"
                item.save(update_fields=["status"])
        source.offset += material.WINDOW
        source.dirty = source.offset < found.locator["length"]
    source.save(update_fields=["dirty", "offset"])


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


def transfer(index, item):
    case = active(index)
    if item.status == "delete":
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
        activity_changed(index)
        return
    if item.status == "recover" or (index.failures and item.status == "queued"):
        row = remote_item(index, item)
        if row:
            receipt(item, row)
            return
    if item.status in ("poll", "check"):
        value = cloudflare.request(
            index, "GET", cloudflare.item_path(item.remote_id), missing_ok=True
        )
        if value is None:
            item.status, item.remote_id = "queued", ""
            item.save(update_fields=["status", "remote_id"])
        else:
            receipt(item, value)
        return
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
        return
    metadata = {
        "pub": str(item.available_at.timestamp()),
        "from_at": str(item.event_start.timestamp()),
        "to_at": str(item.event_end.timestamp()),
        "kind": found.locator["kind"],
        "surface": item.source.surface,
    }
    active(index)
    receipt(
        item,
        cloudflare.request(
            index,
            "POST",
            "/items",
            file=(item.filename, found.text, metadata),
            authorize_transfer=lambda: active(index),
        ),
    )


def step(index):
    if index.retired:
        cloudflare.request(index, "DELETE", missing_ok=True)
        if cloudflare.request(index, "GET", missing_ok=True) is not None:
            raise HostedSearchError("search_cleanup_pending")
        index.delete()
        return
    case = active(index)
    if not index.provisioned:
        cloudflare.ensure_instance(index, lambda: active(index))
        index.provisioned = True
    else:
        sources = HostedSearchSource.objects.filter(index=index)
        items = HostedSearchItem.objects.filter(source__index=index)
        now = timezone.now()
        due = items.filter(
            Q(status__in=("delete", "queued", "recover", "check"))
            | Q(status="poll", checked_at__lt=now - timedelta(seconds=30))
        )
        pending = due.select_related("source").order_by("checked_at", "pk").first()
        source = sources.filter(dirty=True).order_by("id").first()
        if pending:
            transfer(index, pending)
        elif source:
            prepare_window(index, case, source)
        elif index.phase < len(material.surfaces()):
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
        elif items.filter(status="poll").exists():
            enqueue(index, delay=30)
            return
        else:
            index.status = (
                "failed" if items.filter(status="error").exists() else "ready"
            )
            index.error_code = "search_item_failed" if index.status == "failed" else ""
            index.save()
            activity_changed(index)
            return
    index.failures = 0
    index.error_code = ""
    enqueue(index)


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
            step(index)
        except (HostedSearchError, PermissionDenied, Http404) as exc:
            index.failures += 1
            index.error_code = getattr(exc, "code", "search_disabled")
            if index.failures <= 3:
                enqueue(index, delay=(15, 60, 240)[index.failures - 1])
            else:
                index.status = "failed"
                index.save()
                activity_changed(index)
        return index.status

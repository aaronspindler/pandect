"""Browse, add, and review source-neutral catalogue items."""

from collections import defaultdict
from urllib.parse import urlencode
from uuid import UUID

from django.core import signing
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from pandect.catalog.forms import INTAKE_FORMS, METADATA_FORMS, BulkMediaForm
from pandect.catalog.models import CatalogItem
from pandect.catalog.services import create_uploaded_media_batch

REVIEW_TOKEN_SALT = "pandect.catalogue.metadata-review"
BACKFILL_CURSOR_SALT = "pandect.catalogue.processing-backfill"
CATALOGUE_BACKFILL_LIMIT = 50

ITEM_TYPE_DESCRIPTIONS = {
    CatalogItem.ItemType.IMAGE: "An original image file and its media facts.",
    CatalogItem.ItemType.VIDEO: "An original video file and its media facts.",
    CatalogItem.ItemType.AUDIO: "An original audio file and its media facts.",
    CatalogItem.ItemType.EVENT: "An occurrence with optional start and end times.",
    CatalogItem.ItemType.NOTE: "A plain-text observation made by the operator.",
    CatalogItem.ItemType.TEXT_MESSAGE: "A communication whose primary content is text.",
    CatalogItem.ItemType.IMAGE_MESSAGE: "A communication that uses a catalogued image.",
    CatalogItem.ItemType.VIDEO_MESSAGE: "A communication that uses a catalogued video.",
}


def _related(instance, name):
    """Return an optional one-to-one relation without leaking model exceptions."""
    try:
        return getattr(instance, name)
    except ObjectDoesNotExist:
        return None


def _catalogue_card(item: CatalogItem) -> dict[str, object]:
    """Prepare a compact, source-neutral summary for one catalogue item."""
    item_type = item.item_type
    preview = ""
    context = ""
    ended_at = None

    if item_type == CatalogItem.ItemType.IMAGE:
        image = _related(item, "image")
        if image:
            preview = image.original_filename or image.file.name
            dimensions = (
                f"{image.width} × {image.height}"
                if image.width is not None and image.height is not None
                else ""
            )
            context = " · ".join(
                value for value in (image.mime_type, dimensions) if value
            )
    elif item_type == CatalogItem.ItemType.VIDEO:
        video = _related(item, "video")
        if video:
            preview = video.original_filename or video.file.name
            dimensions = (
                f"{video.width} × {video.height}"
                if video.width is not None and video.height is not None
                else ""
            )
            duration = str(video.duration) if video.duration is not None else ""
            context = " · ".join(
                value for value in (video.mime_type, dimensions, duration) if value
            )
    elif item_type == CatalogItem.ItemType.AUDIO:
        audio = _related(item, "audio")
        if audio:
            preview = audio.original_filename or audio.file.name
            duration = str(audio.duration) if audio.duration is not None else ""
            context = " · ".join(
                value for value in (audio.mime_type, duration) if value
            )
    elif item_type == CatalogItem.ItemType.EVENT:
        event = _related(item, "event")
        if event:
            preview = event.description
            ended_at = event.ended_at
    elif item_type == CatalogItem.ItemType.NOTE:
        note = _related(item, "note")
        if note:
            preview = note.body
    else:
        message = _related(item, "message")
        if message:
            preview = message.body
            context = f"From {message.sender}" if message.sender else "Sender unknown"

    item_type_label = item.get_item_type_display()
    return {
        "item": item,
        "title": item.title or f"Untitled {item_type_label.lower()}",
        "type_label": item_type_label,
        "preview": preview,
        "context": context,
        "ended_at": ended_at,
    }


def _backfill_cursor(item_type: str, item: CatalogItem) -> str:
    """Sign one stable catalogue-order position and its active type filter."""
    return signing.dumps(
        {
            "item_type": item_type,
            "catalogued_at": item.catalogued_at.isoformat(),
            "item_id": str(item.pk),
        },
        salt=BACKFILL_CURSOR_SALT,
        compress=True,
    )


def _backfill_position(token: str, item_type: str):
    """Return a validated `(catalogued_at, id)` continuation position."""
    if not token:
        return None
    if len(token) > 1024:
        raise ValueError("invalid backfill cursor")
    try:
        payload = signing.loads(token, salt=BACKFILL_CURSOR_SALT)
        if not isinstance(payload, dict) or payload.get("item_type") != item_type:
            raise ValueError
        catalogued_at = parse_datetime(payload["catalogued_at"])
        item_id = UUID(payload["item_id"])
        if catalogued_at is None or not timezone.is_aware(catalogued_at):
            raise ValueError
    except (signing.BadSignature, KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid backfill cursor") from error
    return catalogued_at, item_id


@require_GET
def catalogue(request: HttpRequest) -> HttpResponse:
    """List catalogue items chronologically with an optional type filter."""
    from pandect.processing.services import LOCAL_STAGE_DEFINITIONS

    item_types = [
        {"value": value, "label": label}
        for value, label in CatalogItem.ItemType.choices
    ]
    valid_types = {item_type["value"] for item_type in item_types}
    active_type = request.GET.get("type", "")
    if active_type not in valid_types:
        active_type = ""

    items = CatalogItem.objects.select_related(
        "image",
        "video",
        "audio",
        "event",
        "note",
        "message__sender",
    )
    if active_type:
        items = items.filter(item_type=active_type)

    page = Paginator(items, 50).get_page(request.GET.get("page"))
    cards = [_catalogue_card(item) for item in page.object_list]
    active_type_label = next(
        (
            item_type["label"]
            for item_type in item_types
            if item_type["value"] == active_type
        ),
        "",
    )
    try:
        added_count = max(0, int(request.GET.get("added", "0")))
    except ValueError:
        added_count = 0
    try:
        processing_added_count = max(0, int(request.GET.get("processing_added", "0")))
        processing_current_count = max(
            0, int(request.GET.get("processing_current", "0"))
        )
        processing_examined_count = max(
            0, int(request.GET.get("processing_examined", "0"))
        )
        processing_skipped_count = max(
            0, int(request.GET.get("processing_skipped", "0"))
        )
    except ValueError:
        processing_added_count = 0
        processing_current_count = 0
        processing_examined_count = 0
        processing_skipped_count = 0
    raw_processing_cursor = request.GET.get("processing_cursor", "")
    try:
        _backfill_position(raw_processing_cursor, active_type)
    except ValueError:
        processing_cursor = ""
    else:
        processing_cursor = raw_processing_cursor

    return render(
        request,
        "catalog/catalogue.html",
        {
            "active_type": active_type,
            "active_type_label": active_type_label,
            "added_count": added_count,
            "processing_added_count": processing_added_count,
            "processing_current_count": processing_current_count,
            "processing_examined_count": processing_examined_count,
            "processing_skipped_count": processing_skipped_count,
            "processing_scan_complete": request.GET.get("processing_complete") == "1",
            "processing_cursor": processing_cursor,
            "processing_backfill_available": (
                not active_type or active_type in LOCAL_STAGE_DEFINITIONS
            ),
            "cards": cards,
            "item_types": item_types,
            "page": page,
        },
    )


@require_POST
def backfill_catalogue_processing(request: HttpRequest) -> HttpResponse:
    """Create a bounded set of append-only local processing runs."""
    from pandect.processing.models import ProcessingRun
    from pandect.processing.services import (
        LOCAL_STAGE_DEFINITIONS,
        PROCESSOR_VERSION,
        ProcessingError,
        create_local_run,
        local_processing_configuration_snapshot,
        source_fingerprint,
    )

    item_type = request.POST.get("item_type", "").strip()
    try:
        limit = int(request.POST.get("limit", str(CATALOGUE_BACKFILL_LIMIT)))
    except ValueError:
        return HttpResponseBadRequest("The backfill limit must be a number.")
    if not 1 <= limit <= CATALOGUE_BACKFILL_LIMIT:
        return HttpResponseBadRequest(
            f"The backfill limit must be between 1 and {CATALOGUE_BACKFILL_LIMIT}."
        )
    if item_type and item_type not in LOCAL_STAGE_DEFINITIONS:
        return HttpResponseBadRequest("That catalogue type cannot be processed.")
    raw_cursor = request.POST.get("cursor", "")
    try:
        position = _backfill_position(raw_cursor, item_type)
    except ValueError:
        return HttpResponseBadRequest("The backfill cursor is not valid.")

    processable_types = tuple(LOCAL_STAGE_DEFINITIONS)
    items = CatalogItem.objects.select_related(
        "message",
        "image",
        "video",
        "audio",
    ).filter(item_type__in=processable_types)
    if item_type:
        items = items.filter(item_type=item_type)
    if position is not None:
        catalogued_at, item_id = position
        items = items.filter(
            Q(catalogued_at__gt=catalogued_at)
            | Q(catalogued_at=catalogued_at, id__gt=item_id)
        )
    window = list(items.order_by("catalogued_at", "id")[: limit + 1])
    selected = window[:limit]
    has_more = len(window) > limit

    runs_by_item = defaultdict(list)
    for run in (
        ProcessingRun.objects.filter(
            catalog_item_id__in=[item.pk for item in selected],
            scope=ProcessingRun.Scope.LOCAL,
            processor_version=PROCESSOR_VERSION,
        )
        .exclude(status=ProcessingRun.Status.SUPERSEDED)
        .only(
            "catalog_item_id",
            "source_fingerprint",
            "source_size",
            "configuration",
        )
    ):
        runs_by_item[run.catalog_item_id].append(run)
    snapshots = {
        selected_type: local_processing_configuration_snapshot(selected_type)
        for selected_type in {item.item_type for item in selected}
    }

    created = 0
    current = 0
    skipped = 0
    for item in selected:
        try:
            fingerprint = source_fingerprint(item)
        except ProcessingError:
            skipped += 1
            continue
        snapshot = snapshots[item.item_type]
        if any(
            run.source_fingerprint == fingerprint.sha256
            and run.source_size == fingerprint.size_bytes
            and isinstance(run.configuration, dict)
            and run.configuration.get("workflow", "") != "identity_gallery_rematch"
            and run.configuration.get("local_processing_snapshot") == snapshot
            for run in runs_by_item[item.pk]
        ):
            current += 1
            continue
        try:
            create_local_run(item.pk, trigger=ProcessingRun.Trigger.BACKFILL)
        except ProcessingError:
            skipped += 1
        else:
            created += 1

    next_cursor = _backfill_cursor(item_type, selected[-1]) if selected else raw_cursor
    query = {
        "processing_added": created,
        "processing_current": current,
        "processing_examined": len(selected),
        "processing_skipped": skipped,
        "processing_complete": int(not has_more),
    }
    if next_cursor:
        query["processing_cursor"] = next_cursor
    if item_type:
        query["type"] = item_type
    return redirect(f"{reverse('catalogue')}?{urlencode(query)}")


def _review_token(item_ids) -> str:
    return signing.dumps(
        {"item_ids": [str(item_id) for item_id in item_ids]},
        salt=REVIEW_TOKEN_SALT,
        compress=True,
    )


def _review_item_ids(token: str) -> list[str]:
    try:
        payload = signing.loads(token, salt=REVIEW_TOKEN_SALT)
        item_ids = payload["item_ids"]
        if not isinstance(item_ids, list) or not item_ids:
            raise TypeError
        return [str(item_id) for item_id in item_ids]
    except (signing.BadSignature, KeyError, TypeError) as error:
        raise Http404("This metadata review sequence is not valid.") from error


def _review_url(item_id, token: str) -> str:
    path = reverse("catalogue-review-metadata", kwargs={"item_id": item_id})
    return f"{path}?{urlencode({'batch': token})}"


def _start_review(items):
    item_ids = [item.pk for item in items]
    token = _review_token(item_ids)
    return redirect(_review_url(item_ids[0], token))


@require_GET
def add_catalogue(request: HttpRequest) -> HttpResponse:
    """Choose one explicit catalogue item type or the media batch flow."""
    item_types = [
        {
            "value": value,
            "label": label,
            "description": ITEM_TYPE_DESCRIPTIONS[value],
        }
        for value, label in CatalogItem.ItemType.choices
    ]
    return render(
        request,
        "catalog/add_catalogue.html",
        {"item_types": item_types},
    )


@require_http_methods(["GET", "POST"])
def add_catalogue_item(request: HttpRequest, item_type: str) -> HttpResponse:
    """Capture the primary content required to construct one typed item."""
    form_class = INTAKE_FORMS.get(item_type)
    try:
        item_type_choice = CatalogItem.ItemType(item_type)
    except ValueError as error:
        raise Http404("Unknown catalogue item type.") from error
    if form_class is None:
        raise Http404("Unknown catalogue item type.")

    form = form_class(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        try:
            item = form.create()
        except ValueError as error:
            form.add_error(None, str(error))
        else:
            return _start_review([item])

    return render(
        request,
        "catalog/add_item.html",
        {
            "form": form,
            "item_type": item_type_choice,
            "item_type_description": ITEM_TYPE_DESCRIPTIONS[item_type_choice],
        },
    )


@require_http_methods(["GET", "POST"])
def bulk_add_catalogue(request: HttpRequest) -> HttpResponse:
    """Add verified local media, then review each item's metadata."""
    form = BulkMediaForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        try:
            items = create_uploaded_media_batch(
                form.cleaned_data["files"], inspections=form.inspections
            )
        except ValueError as error:
            form.add_error("files", str(error))
        else:
            return _start_review(items)
    return render(request, "catalog/bulk_add.html", {"form": form})


def _next_review_item(item_ids: list[str], current_id: str):
    current_index = item_ids.index(current_id)
    remaining_ids = item_ids[current_index + 1 :]
    existing = set(
        str(item_id)
        for item_id in CatalogItem.objects.filter(pk__in=remaining_ids).values_list(
            "pk", flat=True
        )
    )
    return next((item_id for item_id in remaining_ids if item_id in existing), None)


@require_http_methods(["GET", "POST"])
def review_catalogue_metadata(request: HttpRequest, item_id) -> HttpResponse:
    """Walk one signed intake sequence through explicit per-type metadata forms."""
    token = request.POST.get("batch") or request.GET.get("batch", "")
    item_ids = _review_item_ids(token)
    current_id = str(item_id)
    if current_id not in item_ids:
        raise Http404("This item is not part of the metadata review sequence.")

    item = (
        CatalogItem.objects.select_related(
            "image",
            "video",
            "audio",
            "event",
            "note",
            "message__sender",
        )
        .filter(pk=item_id)
        .first()
    )
    if item is None:
        raise Http404("Catalogue item not found.")

    form_class = METADATA_FORMS[item.item_type]
    form = form_class(request.POST if request.method == "POST" else None, item=item)
    if request.method == "POST" and request.POST.get("action") != "skip":
        if form.is_valid():
            form.save()
        else:
            return render(
                request,
                "catalog/review_metadata.html",
                _metadata_review_context(item, form, item_ids, token),
            )

    if request.method == "POST":
        next_item_id = _next_review_item(item_ids, current_id)
        if next_item_id:
            return redirect(_review_url(next_item_id, token))
        return redirect(f"{reverse('catalogue')}?{urlencode({'added': len(item_ids)})}")

    return render(
        request,
        "catalog/review_metadata.html",
        _metadata_review_context(item, form, item_ids, token),
    )


def _metadata_review_context(item, form, item_ids, token):
    position = item_ids.index(str(item.pk)) + 1
    return {
        "batch_token": token,
        "card": _catalogue_card(item),
        "form": form,
        "item": item,
        "position": position,
        "total": len(item_ids),
    }

"""Read-only views for browsing the investigation catalogue."""

from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from pandect.catalog.models import CatalogItem


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


@require_GET
def catalogue(request: HttpRequest) -> HttpResponse:
    """List catalogue items chronologically with an optional type filter."""
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

    return render(
        request,
        "catalog/catalogue.html",
        {
            "active_type": active_type,
            "active_type_label": active_type_label,
            "cards": cards,
            "item_types": item_types,
            "page": page,
        },
    )

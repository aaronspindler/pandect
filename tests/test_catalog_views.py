from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.messages.models import CommunicationParticipant, Message, TextMessage
from pandect.notes.models import Note
from pandect.photos.models import Image

pytestmark = pytest.mark.django_db


def create_item(item_type, **fields):
    return CatalogItem.objects.create(item_type=item_type, **fields)


def test_catalogue_renders_readable_summaries_in_chronological_order(client):
    now = timezone.now()
    sender = CommunicationParticipant.objects.create(display_name="Morgan Lee")
    note_item = create_item(
        CatalogItem.ItemType.NOTE,
        title="First observation",
        occurred_at=now - timedelta(hours=1),
    )
    Note.objects.create(catalog_item=note_item, body="A useful operator note.")
    image_item = create_item(
        CatalogItem.ItemType.IMAGE,
        title="Doorway photograph",
        occurred_at=now,
    )
    Image.objects.create(
        catalog_item=image_item,
        original_filename="doorway.jpg",
        mime_type="image/jpeg",
        width=1600,
        height=900,
    )
    message_item = create_item(CatalogItem.ItemType.TEXT_MESSAGE)
    message = Message.objects.create(
        catalog_item=message_item,
        body="Meet by the north entrance.",
        sender=sender,
    )
    TextMessage.objects.create(message=message)

    response = client.get(reverse("catalogue"))

    assert response.status_code == 200
    assert response.templates[0].name == "catalog/catalogue.html"
    content = response.content.decode()
    assert "Collected material" in content
    assert "First observation" in content
    assert "A useful operator note." in content
    assert "Doorway photograph" in content
    assert "doorway.jpg" in content
    assert "image/jpeg · 1600 × 900" in content
    assert "Untitled text message" in content
    assert "Meet by the north entrance." in content
    assert "From Morgan Lee" in content
    assert content.index("First observation") < content.index("Doorway photograph")
    assert "<strong>3</strong>" in content


def test_catalogue_filters_items_by_the_requested_type(client):
    note_item = create_item(CatalogItem.ItemType.NOTE, title="Included note")
    Note.objects.create(catalog_item=note_item, body="Included body")
    create_item(CatalogItem.ItemType.EVENT, title="Excluded event")

    response = client.get(reverse("catalogue"), {"type": "note"})

    content = response.content.decode()
    assert response.status_code == 200
    assert "Included note" in content
    assert "Excluded event" not in content
    assert "<strong>1</strong>" in content
    assert "item · Note" in content
    assert 'href="?type=note" aria-current="page"' in content


def test_catalogue_ignores_an_unknown_type_filter(client):
    item = create_item(CatalogItem.ItemType.NOTE, title="Still visible")
    Note.objects.create(catalog_item=item, body="Note body")

    response = client.get(reverse("catalogue"), {"type": "not-a-type"})

    assert response.status_code == 200
    assert "Still visible" in response.content.decode()
    assert response.context["active_type"] == ""


def test_catalogue_has_clear_empty_states(client):
    response = client.get(reverse("catalogue"))

    assert "The catalogue is empty" in response.content.decode()

    item = create_item(CatalogItem.ItemType.NOTE)
    Note.objects.create(catalog_item=item, body="A note")
    filtered_response = client.get(reverse("catalogue"), {"type": "video"})

    filtered_content = filtered_response.content.decode()
    assert "No video items" in filtered_content
    assert 'href="/catalogue/">View all catalogue items</a>' in filtered_content


def test_catalogue_fetches_one_page_without_per_item_queries(
    client,
    django_assert_num_queries,
):
    for number in range(3):
        item = create_item(CatalogItem.ItemType.NOTE, title=f"Note {number}")
        Note.objects.create(catalog_item=item, body=f"Body {number}")

    with django_assert_num_queries(2):
        response = client.get(reverse("catalogue"))

    assert response.status_code == 200


def test_catalogue_paginates_long_result_sets_and_keeps_the_filter(client):
    CatalogItem.objects.bulk_create(
        [
            CatalogItem(item_type=CatalogItem.ItemType.NOTE, title=f"Note {number}")
            for number in range(51)
        ]
    )

    response = client.get(reverse("catalogue"), {"type": "note"})

    content = response.content.decode()
    assert "Page 1 of 2" in content
    assert 'href="?type=note&amp;page=2">Next</a>' in content
    assert len(response.context["cards"]) == 50

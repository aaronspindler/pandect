from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from pandect.catalog.models import CatalogItem
from pandect.messages.models import (
    CommunicationParticipant,
    ImageMessage,
    Message,
    MessageParticipant,
    TextMessage,
    VideoMessage,
)
from pandect.notes.models import Note
from pandect.photos.models import Image
from pandect.processing.models import ProcessingRun
from pandect.videos.models import Video

pytestmark = pytest.mark.django_db


def create_item(item_type, **fields):
    return CatalogItem.objects.create(item_type=item_type, **fields)


def create_text_message_item(body: str):
    item = create_item(CatalogItem.ItemType.TEXT_MESSAGE)
    message = Message.objects.create(catalog_item=item, body=body)
    TextMessage.objects.create(message=message)
    return item


def png_upload(name="evidence.png", *, width=16, height=9):
    contents = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )
    return SimpleUploadedFile(name, contents, content_type="image/png")


def mp4_upload(name="evidence.mp4"):
    return SimpleUploadedFile(
        name,
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom",
        content_type="video/mp4",
    )


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
    assert 'href="/catalogue/" aria-current="page">Catalogue</a>' in content
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
    assert 'href="/catalogue/add/">Add your first item</a>' in response.content.decode()
    assert "Process existing material" not in response.content.decode()

    item = create_item(CatalogItem.ItemType.NOTE)
    Note.objects.create(catalog_item=item, body="A note")
    filtered_response = client.get(reverse("catalogue"), {"type": "video"})

    filtered_content = filtered_response.content.decode()
    assert "No video items" in filtered_content
    assert 'href="/catalogue/">View all catalogue items</a>' in filtered_content


def test_catalogue_previews_are_short_without_truncating_saved_content(client):
    body = "A lengthy observation. " * 30
    item = create_item(CatalogItem.ItemType.NOTE, title="Long observation")
    Note.objects.create(catalog_item=item, body=body)

    catalogue = client.get(reverse("catalogue"))
    detail = client.get(reverse("catalogue-item-detail", args=(item.pk,)))

    assert body not in catalogue.content.decode()
    assert body in detail.content.decode()
    item.note.refresh_from_db()
    assert item.note.body == body


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


def test_catalogue_backfill_action_is_bounded_and_type_scoped(client, monkeypatch):
    processable = create_text_message_item("Process this message.")
    ignored = create_item(CatalogItem.ItemType.NOTE)
    created = []

    def fake_create(item_id, *, trigger):
        created.append((item_id, trigger))

    monkeypatch.setattr(
        "pandect.processing.services.create_local_run",
        fake_create,
    )

    response = client.post(
        reverse("catalogue-processing-backfill"),
        {"item_type": CatalogItem.ItemType.TEXT_MESSAGE, "limit": "1"},
    )

    assert response.status_code == 302
    assert created == [(processable.pk, "backfill")]
    assert str(ignored.pk) not in response["Location"]
    assert "processing_added=1" in response["Location"]
    assert "type=text_message" in response["Location"]


def test_catalogue_backfill_cursor_advances_past_fifty_current_items(
    client,
    monkeypatch,
):
    from pandect.processing.services import create_local_run

    for number in range(65):
        create_text_message_item(f"Backfill message {number}.")
    # Exercise the UUID tie-breaker as well as the timestamp cursor.
    CatalogItem.objects.filter(item_type=CatalogItem.ItemType.TEXT_MESSAGE).update(
        catalogued_at=timezone.now()
    )
    ordered = list(
        CatalogItem.objects.filter(item_type=CatalogItem.ItemType.TEXT_MESSAGE)
        .order_by("catalogued_at", "id")
        .values_list("pk", flat=True)
    )
    for item_id in ordered[:50]:
        create_local_run(item_id, enqueue=False)

    created = []

    def fake_create(item_id, *, trigger):
        created.append((item_id, trigger))

    monkeypatch.setattr(
        "pandect.processing.services.create_local_run",
        fake_create,
    )
    first = client.post(
        reverse("catalogue-processing-backfill"),
        {"item_type": CatalogItem.ItemType.TEXT_MESSAGE, "limit": "50"},
    )

    first_query = parse_qs(urlsplit(first["Location"]).query)
    assert first.status_code == 302
    assert first_query["processing_examined"] == ["50"]
    assert first_query["processing_current"] == ["50"]
    assert first_query["processing_added"] == ["0"]
    assert first_query["processing_complete"] == ["0"]
    assert created == []
    continued_page = client.get(first["Location"])
    continued_content = continued_page.content.decode()
    assert "Check next 50 text message items" in continued_content
    assert (
        f'name="cursor" value="{first_query["processing_cursor"][0]}"'
        in continued_content
    )

    second = client.post(
        reverse("catalogue-processing-backfill"),
        {
            "item_type": CatalogItem.ItemType.TEXT_MESSAGE,
            "limit": "50",
            "cursor": first_query["processing_cursor"][0],
        },
    )

    second_query = parse_qs(urlsplit(second["Location"]).query)
    assert second.status_code == 302
    assert second_query["processing_examined"] == ["15"]
    assert second_query["processing_added"] == ["15"]
    assert second_query["processing_complete"] == ["1"]
    assert created == [
        (item_id, ProcessingRun.Trigger.BACKFILL) for item_id in ordered[50:]
    ]


def test_catalogue_backfill_reprocesses_after_configuration_change(
    client,
    monkeypatch,
    settings,
):
    from pandect.processing.services import create_local_run

    item = create_text_message_item("Configuration-bound message.")
    create_local_run(item, enqueue=False)
    settings.PROCESSING_MAX_IMAGE_PIXELS += 1
    created = []

    def fake_create(item_id, *, trigger):
        created.append((item_id, trigger))

    monkeypatch.setattr(
        "pandect.processing.services.create_local_run",
        fake_create,
    )

    response = client.post(
        reverse("catalogue-processing-backfill"),
        {"item_type": CatalogItem.ItemType.TEXT_MESSAGE, "limit": "1"},
    )

    assert response.status_code == 302
    assert created == [(item.pk, ProcessingRun.Trigger.BACKFILL)]
    assert "processing_added=1" in response["Location"]
    assert "processing_current=0" in response["Location"]


def test_catalogue_backfill_reprocesses_after_source_change(client, monkeypatch):
    from pandect.processing.services import create_local_run

    item = create_text_message_item("Original source body.")
    create_local_run(item, enqueue=False)
    Message.objects.filter(catalog_item=item).update(body="Replacement source body.")
    created = []

    def fake_create(item_id, *, trigger):
        created.append((item_id, trigger))

    monkeypatch.setattr(
        "pandect.processing.services.create_local_run",
        fake_create,
    )

    response = client.post(
        reverse("catalogue-processing-backfill"),
        {"item_type": CatalogItem.ItemType.TEXT_MESSAGE, "limit": "1"},
    )

    assert response.status_code == 302
    assert created == [(item.pk, ProcessingRun.Trigger.BACKFILL)]
    assert "processing_current=0" in response["Location"]


def test_catalogue_backfill_cursor_is_bound_to_the_type_filter(client):
    create_text_message_item("Cursor-bound message.")
    first = client.post(
        reverse("catalogue-processing-backfill"),
        {"item_type": CatalogItem.ItemType.TEXT_MESSAGE, "limit": "1"},
    )
    cursor = parse_qs(urlsplit(first["Location"]).query)["processing_cursor"][0]

    response = client.post(
        reverse("catalogue-processing-backfill"),
        {
            "item_type": CatalogItem.ItemType.IMAGE,
            "limit": "1",
            "cursor": cursor,
        },
    )

    assert response.status_code == 400


def test_catalogue_hides_backfill_for_unprocessable_type_filter(client):
    item = create_item(CatalogItem.ItemType.NOTE)
    Note.objects.create(catalog_item=item, body="Operator note")

    response = client.get(reverse("catalogue"), {"type": CatalogItem.ItemType.NOTE})

    assert reverse("catalogue-processing-backfill") not in response.content.decode()


@pytest.mark.parametrize("limit", ("0", "51", "not-a-number"))
def test_catalogue_backfill_action_rejects_unbounded_limits(client, limit):
    response = client.post(
        reverse("catalogue-processing-backfill"),
        {"item_type": CatalogItem.ItemType.TEXT_MESSAGE, "limit": limit},
    )

    assert response.status_code == 400


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


def test_add_landing_lists_every_type_and_bulk_path(client):
    response = client.get(reverse("catalogue-add"))

    assert response.status_code == 200
    content = response.content.decode()
    for _, label in CatalogItem.ItemType.choices:
        assert label in content
    assert reverse("catalogue-bulk-add") in content


def test_single_image_add_preserves_file_and_prefills_extracted_metadata(client):
    response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "image"}),
        {"file": png_upload("camera/frame.png", width=640, height=480)},
    )

    assert response.status_code == 302
    item = CatalogItem.objects.get()
    image = item.image
    assert item.item_type == CatalogItem.ItemType.IMAGE
    assert item.title == "frame"
    assert image.original_filename == "frame.png"
    assert image.mime_type == "image/png"
    assert (image.width, image.height) == (640, 480)
    with image.file.open("rb") as stored_file:
        assert stored_file.read().startswith(b"\x89PNG\r\n\x1a\n")

    review = client.get(response["Location"])
    assert review.status_code == 200
    assert review.context["position"] == 1
    assert review.context["total"] == 1
    assert review.context["form"]["width"].value() == 640
    assert "Not provided" in review.content.decode()


@pytest.mark.parametrize(
    ("item_type", "post_data", "expected_model"),
    [
        ("event", {"description": "Observed arrival"}, "event"),
        ("note", {"body": "Operator note"}, "note"),
        ("text_message", {"body": "Meet outside"}, "text_message"),
    ],
)
def test_single_textual_types_are_created_before_metadata_review(
    client, item_type, post_data, expected_model
):
    response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": item_type}), post_data
    )

    assert response.status_code == 302
    item = CatalogItem.objects.get()
    if expected_model == "event":
        assert item.event.description == post_data["description"]
    elif expected_model == "note":
        assert item.note.body == post_data["body"]
    else:
        assert item.message.text_message.message_id == item.pk
    assert response["Location"].startswith(
        reverse("catalogue-review-metadata", kwargs={"item_id": item.pk})
    )


def test_media_message_intake_reuses_catalogued_primary_media(client):
    image = Image.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.IMAGE, title="Source image")
    )
    video = Video.objects.create(
        catalog_item=create_item(CatalogItem.ItemType.VIDEO, title="Source video")
    )

    image_response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "image_message"}),
        {"image": image.pk, "body": "Image caption"},
    )
    video_response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "video_message"}),
        {"video": video.pk, "body": "Video caption"},
    )

    assert image_response.status_code == 302
    assert video_response.status_code == 302
    assert ImageMessage.objects.get().image == image
    assert VideoMessage.objects.get().video == video


def test_metadata_review_saves_message_participants_and_finishes(client):
    intake_response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "text_message"}),
        {"body": "Original message"},
    )
    review = client.get(intake_response["Location"])
    token = review.context["batch_token"]
    item = CatalogItem.objects.get()

    response = client.post(
        reverse("catalogue-review-metadata", kwargs={"item_id": item.pk}),
        {
            "batch": token,
            "action": "save",
            "title": "Coordinating message",
            "occurred_at": "2026-08-12T14:30",
            "body": "Original message",
            "sender_name": "Morgan Lee",
            "sender_identifier": "morgan@example.test",
            "sender_type": "person",
            "recipients": "Alex Kim | alex@example.test | to",
            "reply_to": "",
        },
    )

    assert response.status_code == 302
    assert parse_qs(urlsplit(response["Location"]).query) == {"added": ["1"]}
    item.refresh_from_db()
    message = item.message
    assert item.title == "Coordinating message"
    assert timezone.is_aware(item.occurred_at)
    assert message.sender.display_name == "Morgan Lee"
    recipient = MessageParticipant.objects.select_related("participant").get()
    assert recipient.role == MessageParticipant.Role.TO
    assert recipient.participant.identifier == "alex@example.test"


def test_event_metadata_can_move_a_complete_interval_forward(client):
    intake_response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "event"}),
        {"description": "Observed meeting"},
    )
    review = client.get(intake_response["Location"])
    token = review.context["batch_token"]
    item = CatalogItem.objects.get()
    review_url = reverse("catalogue-review-metadata", kwargs={"item_id": item.pk})
    shared = {
        "batch": token,
        "action": "save",
        "title": "Meeting",
        "description": "Observed meeting",
    }
    first_response = client.post(
        review_url,
        shared
        | {
            "occurred_at": "2026-08-12T10:00",
            "ended_at": "2026-08-12T11:00",
        },
    )
    second_response = client.post(
        review_url,
        shared
        | {
            "occurred_at": "2026-08-12T12:00",
            "ended_at": "2026-08-12T13:00",
        },
    )

    assert first_response.status_code == 302
    assert second_response.status_code == 302
    item.refresh_from_db()
    assert timezone.localtime(item.occurred_at).hour == 12
    assert timezone.localtime(item.event.ended_at).hour == 13


def test_bulk_add_detects_mixed_media_then_walks_each_item(client, monkeypatch):
    monkeypatch.setattr(
        "pandect.catalog.services._run_ffprobe",
        lambda _upload: {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                }
            ]
        },
    )
    response = client.post(
        reverse("catalogue-bulk-add"),
        {"files": [png_upload("one.png"), mp4_upload("two.mp4")]},
    )

    assert response.status_code == 302
    items = list(CatalogItem.objects.order_by("catalogued_at"))
    assert [item.item_type for item in items] == [
        CatalogItem.ItemType.IMAGE,
        CatalogItem.ItemType.VIDEO,
    ]

    first_review = client.get(response["Location"])
    assert first_review.context["position"] == 1
    assert first_review.context["total"] == 2
    assert (
        'value="1" max="2" aria-label="Metadata review progress: item 1 of 2"'
        in first_review.content.decode()
    )
    token = first_review.context["batch_token"]
    next_response = client.post(
        reverse("catalogue-review-metadata", kwargs={"item_id": items[0].pk}),
        {"batch": token, "action": "skip"},
    )
    assert next_response.status_code == 302
    second_review = client.get(next_response["Location"])
    assert second_review.context["item"].pk == items[1].pk
    assert second_review.context["position"] == 2

    finish_response = client.post(
        reverse("catalogue-review-metadata", kwargs={"item_id": items[1].pk}),
        {"batch": token, "action": "skip"},
    )
    assert parse_qs(urlsplit(finish_response["Location"]).query) == {"added": ["2"]}


def test_bulk_add_rejects_unknown_content_without_partial_catalogue_writes(client):
    response = client.post(
        reverse("catalogue-bulk-add"),
        {
            "files": [
                png_upload("valid.png"),
                SimpleUploadedFile("not-media.txt", b"not media"),
            ]
        },
    )

    assert response.status_code == 200
    assert "not-media.txt" in response.content.decode()
    assert CatalogItem.objects.count() == 0


def test_bulk_add_requires_at_least_one_file(client):
    response = client.post(reverse("catalogue-bulk-add"), {})

    assert response.status_code == 200
    assert "This field is required" in response.content.decode()
    assert 'class="form-error-summary" role="alert"' in response.content.decode()
    assert CatalogItem.objects.count() == 0


def test_metadata_review_rejects_a_tampered_sequence(client):
    item = create_item(CatalogItem.ItemType.NOTE)
    Note.objects.create(catalog_item=item, body="Review me")

    response = client.get(
        reverse("catalogue-review-metadata", kwargs={"item_id": item.pk}),
        {"batch": "not-a-valid-token"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "/catalogue/add/",
        "/catalogue/add/note/",
        "/catalogue/add/bulk/",
    ],
)
def test_intake_explains_the_add_review_catalogue_workflow(client, url):
    response = client.get(url)

    assert response.status_code == 200
    content = response.content.decode()
    assert 'aria-label="Adding material"' in content
    assert 'class="workflow-step is-current" aria-current="step"' in content
    assert "Add material" in content
    assert "Review details" in content
    assert "Browse catalogue" in content


@pytest.mark.parametrize("media_type", ["image", "video"])
def test_media_message_intake_explains_and_links_the_upload_first_step(
    client, media_type
):
    response = client.get(
        reverse("catalogue-add-item", kwargs={"item_type": f"{media_type}_message"})
    )

    content = response.content.decode()
    upload_url = reverse("catalogue-add-item", kwargs={"item_type": media_type})
    assert f'href="{upload_url}">add the original {media_type} first</a>' in content
    assert f"Choose a saved {media_type}" in content
    assert response.context["form"][media_type].field.required
    assert not response.context["form"]["body"].field.required
    assert 'id="id_body_helptext"' in content
    assert 'aria-describedby="id_body_helptext"' in content


def test_intake_errors_link_to_fields_and_preserve_accessible_help(client):
    response = client.post(reverse("catalogue-bulk-add"), {})

    content = response.content.decode()
    assert (
        'href="#id_files">Image, video, and audio files: This field is required.'
        in (content)
    )
    assert 'id="id_files_error"' in content
    assert 'id="id_files_helptext"' in content
    assert 'aria-describedby="id_files_helptext id_files_error"' in content
    assert 'aria-invalid="true"' in content


def test_blank_metadata_is_highlighted_without_treating_zero_as_missing():
    from django import forms
    from django.template.loader import render_to_string

    class MetadataExampleForm(forms.Form):
        count = forms.IntegerField(required=False, initial=0)
        duration = forms.DurationField(required=False, initial=timedelta(0))
        blank = forms.CharField(required=False, initial="")
        unknown = forms.CharField(required=False)

    content = render_to_string(
        "catalog/partials/form_fields.html",
        {"form": MetadataExampleForm(), "highlight_empty": True},
    )
    fields = content.split('<div class="form-field')[1:]

    assert len(fields) == 4
    assert 'value="0"' in fields[0]
    assert 'value="00:00:00"' in fields[1]
    for field in fields[:2]:
        assert "form-field--unknown" not in field
        assert "Not provided" not in field
    for field in fields[2:]:
        assert "form-field--unknown" in field
        assert "Not provided" in field


def test_review_explains_timezone_and_the_saved_item_skip_behavior(client):
    intake_response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "note"}),
        {"body": "Original note"},
    )
    with timezone.override("America/Toronto"):
        review = client.get(intake_response["Location"])

    content = review.content.decode()
    assert "Your item is already saved." in content
    assert "Enter the time in America/Toronto." in content
    assert 'aria-describedby="id_occurred_at_helptext"' in content
    assert 'id="id_occurred_at_helptext"' in content
    assert "Finish without changes" in content
    assert "Edits you typed on this page will not be saved." in content
    assert 'aria-describedby="skip-review-help" formnovalidate' in content
    item = CatalogItem.objects.get()

    response = client.post(
        reverse("catalogue-review-metadata", kwargs={"item_id": item.pk}),
        {
            "batch": review.context["batch_token"],
            "action": "skip",
            "title": "Unsaved title",
            "body": "Unsaved note",
        },
    )

    assert response.status_code == 302
    item.refresh_from_db()
    assert item.title == "Original note"
    assert item.note.body == "Original note"


def test_text_message_review_marks_existing_required_content_as_required(client):
    intake_response = client.post(
        reverse("catalogue-add-item", kwargs={"item_type": "text_message"}),
        {"body": "Message as received"},
    )

    review = client.get(intake_response["Location"])

    assert review.context["form"]["body"].field.required
    assert not review.context["form"]["sender_name"].field.required
    assert 'name="body" cols="40" rows="6" required' in review.content.decode()

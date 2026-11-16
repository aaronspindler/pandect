from django.urls import path

from audio.views import play_attachment
from phone import conversation_views, imazing_views, views

app_name = "phone"
urlpatterns = [
    path(
        "conversations/", conversation_views.conversation_list, name="conversation_list"
    ),
    path(
        "conversations/<uuid:first_person_id>/<uuid:second_person_id>/",
        conversation_views.conversation_detail,
        name="conversation_detail",
    ),
    path("imazing/new/", imazing_views.imazing_upload, name="imazing_upload"),
    path(
        "imazing/<uuid:run_id>/",
        imazing_views.imazing_review,
        name="imazing_review",
    ),
    path(
        "<str:kind>/<uuid:evidence_id>/attachments/<uuid:attachment_id>/play/",
        play_attachment,
        name="attachment_play",
    ),
    path(
        "<str:kind>/<uuid:evidence_id>/attachments/new/",
        views.add_attachment,
        name="add_attachment",
    ),
    path(
        "<str:kind>/<uuid:evidence_id>/attachments/<uuid:attachment_id>/<str:component>/download/",
        views.attachment_download,
        name="attachment_download",
    ),
]

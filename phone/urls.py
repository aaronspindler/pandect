from django.urls import path

from audio.views import play_attachment
from phone import conversation_views, imessage_views, views

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
    path("imessage/new/", imessage_views.imessage_upload, name="imessage_upload"),
    path(
        "imessage/analysis/<uuid:batch_id>/",
        imessage_views.imessage_analysis_status,
        name="imessage_analysis_status",
    ),
    path(
        "imessage/<uuid:run_id>/",
        imessage_views.imessage_review,
        name="imessage_review",
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

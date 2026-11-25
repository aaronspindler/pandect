from django.urls import path

from audio.views import play_attachment
from phone import (
    call_import_views,
    conversation_views,
    imessage_views,
    instance_summary_views,
    instance_views,
    views,
)

app_name = "phone"
urlpatterns = [
    path(
        "conversations/<uuid:first_person_id>/<uuid:second_person_id>/instances/<uuid:instance_id>/grouping/",
        instance_views.grouping_details,
        name="grouping_details",
    ),
    path(
        "conversations/", conversation_views.conversation_list, name="conversation_list"
    ),
    path(
        "conversations/<uuid:first_person_id>/<uuid:second_person_id>/",
        conversation_views.conversation_detail,
        name="conversation_detail",
    ),
    path(
        "conversations/<uuid:first_person_id>/<uuid:second_person_id>/instances/group/",
        instance_views.group_conversation,
        name="group_conversation",
    ),
    path(
        "conversations/<uuid:first_person_id>/<uuid:second_person_id>/instances/<uuid:instance_id>/coverage/",
        instance_summary_views.session_summary_coverage,
        name="session_summary_coverage",
    ),
    path(
        "conversations/<uuid:first_person_id>/<uuid:second_person_id>/instances/summaries/",
        instance_summary_views.instance_summary,
        name="instance_summaries",
    ),
    path(
        "conversations/<uuid:first_person_id>/<uuid:second_person_id>/instances/<uuid:instance_id>/summary/",
        instance_summary_views.instance_summary,
        name="instance_summary",
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
        "call-history/new/",
        call_import_views.call_history_upload,
        name="call_history_upload",
    ),
    path(
        "call-history/<uuid:import_id>/",
        call_import_views.call_history_review,
        name="call_history_review",
    ),
    path(
        "<str:kind>/<uuid:evidence_id>/link-person/",
        call_import_views.link_imported_person,
        name="link_imported_person",
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

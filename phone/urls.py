from django.urls import path

from phone import views

app_name = "phone"
urlpatterns = [
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

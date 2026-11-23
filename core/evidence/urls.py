from django.urls import path

from audio.views import play as audio_play
from core.evidence import views
from core.evidence.chronology import chronology
from core.evidence.packet import packet_picker, packet_review
from document.views import page as document_page

app_name = "evidence"

urlpatterns = [
    path("", views.evidence_list, name="list"),
    path("new/", views.create, name="create"),
    path("bulk/", views.bulk_create, name="bulk_create"),
    path("chronology/", chronology, name="chronology"),
    path("packet/", packet_picker, name="packet"),
    path("packet/review/", packet_review, name="packet_review"),
    path(
        "document/<uuid:evidence_id>/pages/<int:page>/",
        document_page,
        name="document_page",
    ),
    path("audio/<uuid:evidence_id>/play/", audio_play, name="audio_play"),
    path("<str:kind>/<uuid:evidence_id>/", views.detail, name="detail"),
    path("<str:kind>/<uuid:evidence_id>/edit/", views.edit, name="edit"),
    path("<str:kind>/<uuid:evidence_id>/delete/", views.delete, name="delete"),
    path("<str:kind>/<uuid:evidence_id>/download/", views.download, name="download"),
    path("<str:kind>/<uuid:evidence_id>/preview/", views.preview, name="preview"),
    path("<str:kind>/<uuid:evidence_id>/archive/", views.archive, name="archive"),
    path("<str:kind>/<uuid:evidence_id>/restore/", views.restore, name="restore"),
]

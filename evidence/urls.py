from django.urls import path

from . import import_views, views

app_name = "evidence"

urlpatterns = [
    path("", views.evidence_list, name="list"),
    path("new/", views.create, name="create"),
    path("bulk/", views.bulk_create, name="bulk_create"),
    path(
        "vehicle-locations/import/",
        import_views.vehicle_import_upload,
        name="vehicle_import_upload",
    ),
    path(
        "vehicle-locations/import/<uuid:import_id>/",
        import_views.vehicle_import_review,
        name="vehicle_import_review",
    ),
    path("<str:kind>/<uuid:evidence_id>/", views.detail, name="detail"),
    path("<str:kind>/<uuid:evidence_id>/edit/", views.edit, name="edit"),
    path("<str:kind>/<uuid:evidence_id>/delete/", views.delete, name="delete"),
    path("<str:kind>/<uuid:evidence_id>/download/", views.download, name="download"),
    path("<str:kind>/<uuid:evidence_id>/preview/", views.preview, name="preview"),
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
    path("<str:kind>/<uuid:evidence_id>/archive/", views.archive, name="archive"),
    path("<str:kind>/<uuid:evidence_id>/restore/", views.restore, name="restore"),
]

from django.urls import path

from . import views

app_name = "evidence"

urlpatterns = [
    path("", views.evidence_list, name="list"),
    path("new/", views.create, name="create"),
    path("bulk/", views.bulk_create, name="bulk_create"),
    path("<str:kind>/<uuid:evidence_id>/", views.detail, name="detail"),
    path("<str:kind>/<uuid:evidence_id>/edit/", views.edit, name="edit"),
    path("<str:kind>/<uuid:evidence_id>/download/", views.download, name="download"),
    path("<str:kind>/<uuid:evidence_id>/archive/", views.archive, name="archive"),
    path("<str:kind>/<uuid:evidence_id>/restore/", views.restore, name="restore"),
]

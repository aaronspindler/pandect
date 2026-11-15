from django.urls import path

from . import views

app_name = "analysis"
urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.create, name="create"),
    path("rerun-all/", views.rerun_all, name="rerun_all"),
    path(
        "evidence/<str:kind>/<uuid:evidence_id>/",
        views.process_evidence,
        name="process_evidence",
    ),
    path(
        "evidence/<str:kind>/<uuid:evidence_id>/external/",
        views.create,
        name="external_evidence",
    ),
    path("reports/<str:name>/", views.current_report, name="report"),
    path("<uuid:run_id>/", views.detail, name="detail"),
    path("<uuid:run_id>/cancel/", views.cancel, name="cancel"),
    path("<uuid:run_id>/retry/", views.retry, name="retry"),
    path("<uuid:run_id>/review/", views.review, name="review"),
    path("<uuid:run_id>/download/", views.download, name="download"),
    path("<uuid:run_id>/sources/<uuid:source_id>/audio/", views.audio, name="audio"),
    path("<uuid:run_id>/previews/<uuid:artifact_id>/", views.artifact, name="artifact"),
]

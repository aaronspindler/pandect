from django.urls import path

from . import (
    catalogue_views,
    connections,
    import_checks,
    processing,
    reprocessing_views,
    sighting_views,
    views,
)

app_name = "analysis"
urlpatterns = [
    path("connections/", connections.connections, name="connections"),
    path("sightings/", sighting_views.index, name="sighting_groups"),
    path("sightings/new/", sighting_views.create, name="sighting_create"),
    path("sightings/<uuid:run_id>/", sighting_views.detail, name="sighting_detail"),
    path(
        "sightings/<uuid:run_id>/review/",
        sighting_views.review,
        name="sighting_review",
    ),
    path("", views.index, name="index"),
    path("browse/", catalogue_views.browse, name="browse"),
    path("processing/", processing.index, name="processing"),
    path("processing/reprocess/", reprocessing_views.reprocess, name="reprocess"),
    path(
        "processing/<uuid:batch_id>/resume/",
        reprocessing_views.resume,
        name="resume_batch",
    ),
    path(
        "imports/<str:import_type>/<uuid:import_id>/checks/",
        import_checks.start,
        name="import_checks",
    ),
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
    path(
        "<uuid:run_id>/estimate/<int:item_index>/",
        views.select_estimate,
        name="select_estimate",
    ),
    path("<uuid:run_id>/download/", views.download, name="download"),
    path("<uuid:run_id>/sources/<uuid:source_id>/audio/", views.audio, name="audio"),
    path("<uuid:run_id>/previews/<uuid:artifact_id>/", views.artifact, name="artifact"),
]

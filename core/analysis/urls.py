from django.urls import path

from text.claim_comparison_views import confirm as confirm_claim

from . import (
    catalogue_views,
    connections,
    import_checks,
    output_views,
    processing,
    relationship_views,
    report_views,
    reprocessing_views,
    run_views,
    sighting_views,
    submission_views,
    views,
)

app_name = "analysis"
urlpatterns = [
    path("connections/narrative/", relationship_views.index, name="relationships"),
    path(
        "connections/narrative/<uuid:edge_id>/review/",
        relationship_views.review,
        name="relationship_review",
    ),
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
        "processing/retry-failed/", reprocessing_views.retry_failed, name="retry_failed"
    ),
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
    path("new/", submission_views.create, name="create"),
    path("claims/compare/", confirm_claim, name="claim_confirm"),
    path("rerun-all/", reprocessing_views.rerun_all, name="rerun_all"),
    path(
        "evidence/<str:kind>/<uuid:evidence_id>/",
        reprocessing_views.process_evidence,
        name="process_evidence",
    ),
    path(
        "evidence/<str:kind>/<uuid:evidence_id>/external/",
        submission_views.create,
        name="external_evidence",
    ),
    path("reports/<str:name>/", report_views.current_report, name="report"),
    path("<uuid:run_id>/", run_views.detail, name="detail"),
    path("<uuid:run_id>/cancel/", run_views.cancel, name="cancel"),
    path("<uuid:run_id>/acknowledge/", run_views.acknowledge, name="acknowledge"),
    path("<uuid:run_id>/retry/", run_views.retry, name="retry"),
    path(
        "<uuid:run_id>/estimate/<int:item_index>/",
        run_views.select_estimate,
        name="select_estimate",
    ),
    path("<uuid:run_id>/download/", output_views.download, name="download"),
    path(
        "<uuid:run_id>/sources/<uuid:source_id>/audio/",
        output_views.audio,
        name="audio",
    ),
    path(
        "<uuid:run_id>/previews/<uuid:artifact_id>/",
        output_views.artifact,
        name="artifact",
    ),
]

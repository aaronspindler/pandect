from django.urls import path

from . import (
    catalogue_views,
    connections,
    import_checks,
    processing,
    reprocessing_views,
    selection_views,
    synthesis_views,
    views,
)

app_name = "analysis"
urlpatterns = [
    path("connections/", connections.connections, name="connections"),
    path("selections/", selection_views.selection_list, name="selection_list"),
    path("selections/new/", selection_views.selection_create, name="selection_create"),
    path("selections/pick/", selection_views.selection_pick, name="selection_pick"),
    path(
        "selections/<uuid:selection_id>/",
        selection_views.selection_detail,
        name="selection_detail",
    ),
    path(
        "selections/<uuid:selection_id>/edit/",
        selection_views.selection_edit,
        name="selection_edit",
    ),
    path(
        "selections/<uuid:selection_id>/add/",
        selection_views.selection_add,
        name="selection_add",
    ),
    path(
        "selections/<uuid:selection_id>/remove/<int:member_index>/",
        selection_views.selection_remove,
        name="selection_remove",
    ),
    path(
        "selections/<uuid:selection_id>/delete/",
        selection_views.selection_delete,
        name="selection_delete",
    ),
    path(
        "selections/<uuid:selection_id>/brief/",
        selection_views.selection_brief,
        name="selection_brief",
    ),
    path(
        "selections/<uuid:selection_id>/analyze/",
        selection_views.selection_analyze,
        name="selection_analyze",
    ),
    path(
        "selections/<uuid:selection_id>/synthesize/",
        synthesis_views.create,
        name="synthesize",
    ),
    path("", views.index, name="index"),
    path("browse/", catalogue_views.browse, name="browse"),
    path("processing/", processing.index, name="processing"),
    path("processing/reprocess/", reprocessing_views.reprocess, name="reprocess"),
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
    path("<uuid:run_id>/review/", views.review, name="review"),
    path("<uuid:run_id>/download/", views.download, name="download"),
    path("<uuid:run_id>/sources/<uuid:source_id>/audio/", views.audio, name="audio"),
    path("<uuid:run_id>/previews/<uuid:artifact_id>/", views.artifact, name="artifact"),
]

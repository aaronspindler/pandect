from django.urls import path

from geo import import_views, journey_claim_readers, map_views, poi_views

app_name = "geo"
urlpatterns = [
    path(
        "comparisons/<uuid:run_id>/findings/<int:item_index>/passages/<int:ordinal>/",
        journey_claim_readers.passage,
        name="plan_passage",
    ),
    path(
        "comparisons/<uuid:run_id>/groups/<int:index>/",
        journey_claim_readers.group,
        name="plan_group",
    ),
    path("map/", map_views.case_map, name="case_map"),
    path("map/prepare/", map_views.prepare_trips, name="prepare_trips"),
    path("map/trips/<uuid:trip_id>/", map_views.trip_detail, name="trip_detail"),
    path("people/pois/new/", poi_views.poi_save, name="poi_create"),
    path("people/pois/<uuid:poi_id>/", poi_views.poi_detail, name="poi_detail"),
    path("people/pois/<uuid:poi_id>/edit/", poi_views.poi_save, name="poi_edit"),
    path("people/pois/<uuid:poi_id>/delete/", poi_views.poi_delete, name="poi_delete"),
    path(
        "evidence/vehicle-locations/import/",
        import_views.vehicle_import_upload,
        name="vehicle_import_upload",
    ),
    path(
        "evidence/vehicle-locations/import/<uuid:import_id>/",
        import_views.vehicle_import_review,
        name="vehicle_import_review",
    ),
]

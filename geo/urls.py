from django.urls import path

from geo import import_views, map_views, poi_views

app_name = "geo"
urlpatterns = [
    path("map/", map_views.case_map, name="case_map"),
    path("map/locate/", map_views.locate_map_addresses, name="map_locate"),
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

from django.urls import path

from vehicle import views

app_name = "vehicle"

urlpatterns = [
    path("", views.vehicle_list, name="list"),
    path("new/", views.vehicle_create, name="create"),
    path(
        "new/for-import/<uuid:import_id>/",
        views.vehicle_create,
        name="create_for_import",
    ),
    path("<uuid:vehicle_id>/", views.vehicle_detail, name="detail"),
    path("<uuid:vehicle_id>/edit/", views.vehicle_edit, name="edit"),
    path("<uuid:vehicle_id>/delete/", views.vehicle_delete, name="delete"),
]

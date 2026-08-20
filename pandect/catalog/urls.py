"""URL routes for the catalogue interface."""

from django.urls import include, path

from pandect.catalog import views

urlpatterns = [
    path("", views.catalogue, name="catalogue"),
    path(
        "processing/backfill/",
        views.backfill_catalogue_processing,
        name="catalogue-processing-backfill",
    ),
    path("add/", views.add_catalogue, name="catalogue-add"),
    path("add/bulk/", views.bulk_add_catalogue, name="catalogue-bulk-add"),
    path(
        "add/review/<uuid:item_id>/",
        views.review_catalogue_metadata,
        name="catalogue-review-metadata",
    ),
    path(
        "add/<str:item_type>/",
        views.add_catalogue_item,
        name="catalogue-add-item",
    ),
    path("", include("pandect.processing.urls")),
]

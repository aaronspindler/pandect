"""URL routes for the catalogue interface."""

from django.urls import path

from pandect.catalog import views

urlpatterns = [
    path("", views.catalogue, name="catalogue"),
]

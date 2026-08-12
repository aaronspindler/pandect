"""URL configuration for Pandect."""

from django.urls import include, path

from pandect.core import views

urlpatterns = [
    path("", views.home, name="home"),
    path("catalogue/", include("pandect.catalog.urls")),
    path("status/", views.runtime_status, name="runtime-status"),
    path("healthz/", views.health, name="health"),
    path("readyz/", views.readiness, name="readiness"),
]

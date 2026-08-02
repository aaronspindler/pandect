"""URL configuration for Pandect."""

from django.urls import path

from pandect.core import views

urlpatterns = [
    path("", views.home, name="home"),
    path("status/", views.runtime_status, name="runtime-status"),
    path("healthz/", views.health, name="health"),
    path("readyz/", views.readiness, name="readiness"),
]

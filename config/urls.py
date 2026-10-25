"""Application entry points; case and investigation routes are added incrementally."""

from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from .views import health

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="account_login", permanent=False)),
    path("health/", health, name="health"),
    path("accounts/", include("allauth.urls")),
    path("admin/", admin.site.urls),
]

"""Application entry points; case and investigation routes are added incrementally."""

from django.contrib import admin
from django.urls import include, path

from cases.views import invitation_accept

from .views import health, home

urlpatterns = [
    path("", home, name="home"),
    path("health/", health, name="health"),
    path("accounts/", include("allauth.urls")),
    path("cases/", include("cases.urls")),
    path("cases/<uuid:case_id>/people/", include("people.urls")),
    path("cases/<uuid:case_id>/evidence/", include("evidence.urls")),
    path("invitations/<str:token>/", invitation_accept, name="invitation_accept"),
    path("admin/", admin.site.urls),
]

handler403 = "config.views.permission_denied"
handler404 = "config.views.not_found"
handler500 = "config.views.server_error"

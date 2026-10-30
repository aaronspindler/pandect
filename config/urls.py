"""Application entry points; case and investigation routes are added incrementally."""

from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

from cases.views import invitation_accept

from .views import health, home

urlpatterns = [
    path("", home, name="home"),
    path(
        "product/",
        TemplateView.as_view(template_name="marketing/product.html"),
        name="product",
    ),
    path(
        "trust/",
        TemplateView.as_view(template_name="marketing/trust.html"),
        name="trust",
    ),
    path(
        "hosting/",
        TemplateView.as_view(template_name="marketing/hosting.html"),
        name="hosting",
    ),
    path("health/", health, name="health"),
    path("accounts/", include("allauth.urls")),
    path("cases/", include("cases.urls")),
    path("cases/<uuid:case_id>/people/", include("people.urls")),
    path("cases/<uuid:case_id>/vehicles/", include("vehicles.urls")),
    path("cases/<uuid:case_id>/evidence/", include("evidence.urls")),
    path("cases/<uuid:case_id>/analysis/", include("analysis.urls")),
    path("invitations/<str:token>/", invitation_accept, name="invitation_accept"),
    path("admin/", admin.site.urls),
]

handler403 = "config.views.permission_denied"
handler404 = "config.views.not_found"
handler500 = "config.views.server_error"

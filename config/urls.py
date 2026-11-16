"""Application entry points; case and investigation routes are added incrementally."""

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.urls import include, path

from accounts.views import timezone_settings
from cases.views import invitation_accept

from .views import health

admin.site.login = secure_admin_login(admin.site.login)

urlpatterns = [
    path("", include("marketing.urls")),
    path("health/", health, name="health"),
    path("accounts/timezone/", timezone_settings, name="account_timezone"),
    path("accounts/", include("allauth.urls")),
    path("cases/", include("cases.urls")),
    path("cases/<uuid:case_id>/people/", include("people.urls")),
    path("cases/<uuid:case_id>/vehicles/", include("vehicle.urls")),
    path("cases/<uuid:case_id>/", include("geo.urls")),
    path("cases/<uuid:case_id>/evidence/", include("phone.urls")),
    path("cases/<uuid:case_id>/people/", include("socialmedia.urls")),
    path("cases/<uuid:case_id>/", include("core.urls")),
    path("invitations/<str:token>/", invitation_accept, name="invitation_accept"),
    path("admin/", admin.site.urls),
]

handler403 = "config.views.permission_denied"
handler404 = "config.views.not_found"
handler500 = "config.views.server_error"

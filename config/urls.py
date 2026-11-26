"""Application entry points; case and investigation routes are added incrementally."""

from allauth.account.decorators import secure_admin_login
from django.contrib import admin
from django.urls import include, path

from accounts.views import timezone_settings
from cases.activity import stream_unavailable
from cases.collaboration_views import invitation_accept
from request_activity.views import request_activity
from socialmedia.monitor_views import connection_settings

from .views import health

admin.site.login = secure_admin_login(admin.site.login)
admin.site.site_header = "Pandect administration"
admin.site.site_title = "Pandect administration"
admin.site.index_title = "Manage this installation"
admin.site.index_template = "request_activity/admin_index.html"

urlpatterns = [
    path("events/cases/<uuid:case_id>/", stream_unavailable, name="case_events"),
    path("", include("marketing.urls")),
    path("health/", health, name="health"),
    path("accounts/timezone/", timezone_settings, name="account_timezone"),
    path("accounts/instagram/", connection_settings, name="account_instagram"),
    path("accounts/", include("allauth.urls")),
    path("cases/", include("cases.urls")),
    path("cases/<uuid:case_id>/people/", include("people.urls")),
    path("cases/<uuid:case_id>/vehicles/", include("vehicle.urls")),
    path("cases/<uuid:case_id>/", include("geo.urls")),
    path("cases/<uuid:case_id>/evidence/", include("phone.urls")),
    path("cases/<uuid:case_id>/people/", include("socialmedia.urls")),
    path("cases/<uuid:case_id>/instagram/", include("socialmedia.monitor_urls")),
    path("cases/<uuid:case_id>/", include("core.urls")),
    path("invitations/<str:token>/", invitation_accept, name="invitation_accept"),
    path(
        "admin/request-activity/",
        admin.site.admin_view(request_activity),
        name="request_activity",
    ),
    path("admin/", admin.site.urls),
]

handler403 = "config.views.permission_denied"
handler404 = "config.views.not_found"
handler500 = "config.views.server_error"

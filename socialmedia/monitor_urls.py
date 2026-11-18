from django.urls import path

from socialmedia import monitor_views as views

app_name = "instagram"
urlpatterns = [
    path("", views.monitor_list, name="list"),
    path("new/", views.monitor_create, name="create"),
    path("<uuid:monitor_id>/", views.monitor_detail, name="detail"),
    path(
        "<uuid:monitor_id>/captures/<uuid:capture_id>/", views.capture, name="capture"
    ),
    path("<uuid:monitor_id>/<str:action>/", views.monitor_action, name="action"),
]

from django.urls import path

from . import views

app_name = "important"
urlpatterns = [
    path("", views.index, name="list"),
    path("<uuid:item_id>/notes/", views.notes, name="notes"),
    path("chat-reply/<uuid:turn_id>/", views.mark_reply, name="mark_reply"),
    path(
        "evidence/<str:kind>/<uuid:evidence_id>/",
        views.mark_evidence,
        name="mark_evidence",
    ),
    path(
        "finding/<uuid:run_id>/<int:item_index>/",
        views.mark_finding,
        name="mark_finding",
    ),
]

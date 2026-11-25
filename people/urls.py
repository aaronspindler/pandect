from django.urls import path

from people import views
from text.writing_views import writing_style

app_name = "people"

urlpatterns = [
    path("", views.person_list, name="list"),
    path("new/", views.person_create, name="create"),
    path("<uuid:person_id>/", views.person_detail, name="detail"),
    path("<uuid:person_id>/writing-style/", writing_style, name="writing_style"),
    path("<uuid:person_id>/edit/", views.person_edit, name="edit"),
    path("<uuid:person_id>/delete/", views.person_delete, name="delete"),
]

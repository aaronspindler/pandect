from django.urls import path

from people import views

app_name = "people"

urlpatterns = [
    path("", views.person_list, name="list"),
    path("new/", views.person_create, name="create"),
    path("<uuid:person_id>/", views.person_detail, name="detail"),
    path("<uuid:person_id>/edit/", views.person_edit, name="edit"),
    path("<uuid:person_id>/delete/", views.person_delete, name="delete"),
]

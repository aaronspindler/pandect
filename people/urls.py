from django.urls import path

from . import views
from .poi_views import poi_delete, poi_detail, poi_save
from .social_account_views import social_account_delete, social_account_save

app_name = "people"

urlpatterns = [
    path("", views.person_list, name="list"),
    path("new/", views.person_create, name="create"),
    path("pois/new/", poi_save, name="poi_create"),
    path("pois/<uuid:poi_id>/", poi_detail, name="poi_detail"),
    path("pois/<uuid:poi_id>/edit/", poi_save, name="poi_edit"),
    path("pois/<uuid:poi_id>/delete/", poi_delete, name="poi_delete"),
    path("<uuid:person_id>/", views.person_detail, name="detail"),
    path("<uuid:person_id>/edit/", views.person_edit, name="edit"),
    path("<uuid:person_id>/delete/", views.person_delete, name="delete"),
    path(
        "<uuid:person_id>/social-accounts/new/",
        social_account_save,
        name="social_account_create",
    ),
    path(
        "<uuid:person_id>/social-accounts/<uuid:account_id>/edit/",
        social_account_save,
        name="social_account_edit",
    ),
    path(
        "<uuid:person_id>/social-accounts/<uuid:account_id>/delete/",
        social_account_delete,
        name="social_account_delete",
    ),
]

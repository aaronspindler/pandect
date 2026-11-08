from django.urls import path

from socialmedia import views

app_name = "socialmedia"
urlpatterns = [
    path(
        "<uuid:person_id>/social-accounts/new/",
        views.social_account_save,
        name="social_account_create",
    ),
    path(
        "<uuid:person_id>/social-accounts/<uuid:account_id>/edit/",
        views.social_account_save,
        name="social_account_edit",
    ),
    path(
        "<uuid:person_id>/social-accounts/<uuid:account_id>/delete/",
        views.social_account_delete,
        name="social_account_delete",
    ),
]

from django.urls import path

from . import views

app_name = "cases"

urlpatterns = [
    path("", views.case_list, name="case_list"),
    path("new/", views.case_create, name="case_create"),
    path("<uuid:case_id>/", views.case_detail, name="case_detail"),
    path("<uuid:case_id>/settings/", views.case_settings, name="case_settings"),
    path("<uuid:case_id>/delete/", views.case_delete, name="case_delete"),
    path(
        "<uuid:case_id>/invitations/new/",
        views.invitation_create,
        name="invitation_create",
    ),
    path(
        "<uuid:case_id>/invitations/<uuid:invitation_id>/edit/",
        views.invitation_edit,
        name="invitation_edit",
    ),
    path(
        "<uuid:case_id>/invitations/<uuid:invitation_id>/resend/",
        views.invitation_resend,
        name="invitation_resend",
    ),
    path(
        "<uuid:case_id>/invitations/<uuid:invitation_id>/revoke/",
        views.invitation_revoke,
        name="invitation_revoke",
    ),
    path(
        "<uuid:case_id>/memberships/<uuid:membership_id>/edit/",
        views.membership_edit,
        name="membership_edit",
    ),
    path(
        "<uuid:case_id>/memberships/<uuid:membership_id>/remove/",
        views.membership_remove,
        name="membership_remove",
    ),
]

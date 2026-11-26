from django.urls import path

from cases.generation_views import update as search_generation

from . import activity, chat_routing_views, rerank_views, views

app_name = "cases"

urlpatterns = [
    path("", views.case_list, name="case_list"),
    path(
        "<uuid:case_id>/settings/chat-routing/",
        chat_routing_views.change,
        name="case_chat_routing",
    ),
    path(
        "<uuid:case_id>/settings/passage-ranking/",
        rerank_views.change,
        name="case_passage_ranking",
    ),
    path("new/", views.case_create, name="case_create"),
    path("<uuid:case_id>/", views.case_detail, name="case_detail"),
    path("<uuid:case_id>/activity/", activity.status, name="activity"),
    path("<uuid:case_id>/settings/", views.case_settings, name="case_settings"),
    path(
        "<uuid:case_id>/settings/search-preparation/",
        search_generation,
        name="search_generation",
    ),
    path(
        "<uuid:case_id>/settings/ai-processing/",
        views.case_ai_settings,
        name="case_ai_settings",
    ),
    path(
        "<uuid:case_id>/settings/external-analysis/",
        views.case_external_analysis,
        name="case_external_analysis",
    ),
    path(
        "<uuid:case_id>/settings/meaning-search/",
        views.case_search_approval,
        name="case_search_approval",
    ),
    path(
        "<uuid:case_id>/settings/instagram-monitoring/",
        views.case_instagram_monitoring,
        name="case_instagram_monitoring",
    ),
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

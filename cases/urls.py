from django.urls import path

from . import activity, collaboration_views, search_views, settings_views, views

app_name = "cases"

urlpatterns = [
    path("", views.case_list, name="case_list"),
    path(
        "<uuid:case_id>/settings/chat-routing/",
        search_views.chat_routing,
        name="case_chat_routing",
    ),
    path(
        "<uuid:case_id>/settings/passage-ranking/",
        search_views.passage_ranking,
        name="case_passage_ranking",
    ),
    path("new/", views.case_create, name="case_create"),
    path("<uuid:case_id>/", views.case_detail, name="case_detail"),
    path("<uuid:case_id>/activity/", activity.status, name="activity"),
    path(
        "<uuid:case_id>/settings/", settings_views.case_settings, name="case_settings"
    ),
    path(
        "<uuid:case_id>/settings/search-preparation/",
        search_views.search_preparation,
        name="search_generation",
    ),
    path(
        "<uuid:case_id>/settings/ai-processing/",
        settings_views.case_ai_settings,
        name="case_ai_settings",
    ),
    path(
        "<uuid:case_id>/settings/external-analysis/",
        settings_views.case_external_analysis,
        name="case_external_analysis",
    ),
    path(
        "<uuid:case_id>/settings/meaning-search/",
        search_views.meaning_search,
        name="case_search_approval",
    ),
    path(
        "<uuid:case_id>/settings/instagram-monitoring/",
        settings_views.case_instagram_monitoring,
        name="case_instagram_monitoring",
    ),
    path("<uuid:case_id>/delete/", views.case_delete, name="case_delete"),
    path(
        "<uuid:case_id>/invitations/new/",
        collaboration_views.invitation_create,
        name="invitation_create",
    ),
    path(
        "<uuid:case_id>/invitations/<uuid:invitation_id>/edit/",
        collaboration_views.invitation_edit,
        name="invitation_edit",
    ),
    path(
        "<uuid:case_id>/invitations/<uuid:invitation_id>/resend/",
        collaboration_views.invitation_resend,
        name="invitation_resend",
    ),
    path(
        "<uuid:case_id>/invitations/<uuid:invitation_id>/revoke/",
        collaboration_views.invitation_revoke,
        name="invitation_revoke",
    ),
    path(
        "<uuid:case_id>/memberships/<uuid:membership_id>/edit/",
        collaboration_views.membership_edit,
        name="membership_edit",
    ),
    path(
        "<uuid:case_id>/memberships/<uuid:membership_id>/remove/",
        collaboration_views.membership_remove,
        name="membership_remove",
    ),
]

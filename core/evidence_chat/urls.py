from django.urls import path

from . import hosted_agent, views

app_name = "chat"
urlpatterns = [
    path("", views.chat, name="list"),
    path("agent/<uuid:turn_id>/", hosted_agent.callback, name="agent_callback"),
    path("<uuid:chat_id>/", views.chat, name="detail"),
    path("<uuid:chat_id>/replies/<uuid:turn_id>/", views.reply, name="reply"),
    path(
        "<uuid:chat_id>/replies/<uuid:turn_id>/selection/",
        views.selection,
        name="selection",
    ),
    path(
        "<uuid:chat_id>/replies/<uuid:turn_id>/proposed-check/",
        views.proposal,
        name="proposal",
    ),
    path(
        "<uuid:chat_id>/replies/<uuid:turn_id>/update/",
        views.update_answer,
        name="update",
    ),
    path("<uuid:chat_id>/replies/<uuid:turn_id>/retry/", views.retry, name="retry"),
]

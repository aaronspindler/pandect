from django.urls import path

from . import views

app_name = "chat"
urlpatterns = [
    path("", views.chat, name="list"),
    path("<uuid:chat_id>/", views.chat, name="detail"),
    path("<uuid:chat_id>/replies/<uuid:turn_id>/", views.reply, name="reply"),
    path("<uuid:chat_id>/replies/<uuid:turn_id>/retry/", views.retry, name="retry"),
]

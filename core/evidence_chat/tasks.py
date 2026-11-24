"""Queue deliveries carry case-chat work identities only."""

from django.tasks import task

from .jobs import process_turn


@task
def process_chat_turn(turn_id, delivery_token):
    process_turn(turn_id, delivery_token)

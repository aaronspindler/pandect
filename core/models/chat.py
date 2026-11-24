"""Saved case conversations and checkpointed, source-bound answers."""

from uuid import uuid4

from django.conf import settings
from django.db import models


class EvidenceChat(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class ChatTurn(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Reviewing evidence"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Needs attention"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    chat = models.ForeignKey(
        EvidenceChat, related_name="turns", on_delete=models.CASCADE
    )
    asked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    ordinal = models.PositiveIntegerField()
    question = models.TextField(max_length=2000)
    include_archived = models.BooleanField(default=False)
    submission_id = models.UUIDField(unique=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    mode = models.CharField(max_length=20, blank=True)
    execution = models.JSONField(default=dict)
    approval_revision = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    answer = models.JSONField(default=dict)
    plan = models.JSONField(default=dict)
    total = models.PositiveBigIntegerField(default=0)
    reviewed = models.PositiveBigIntegerField(default=0)
    gaps = models.PositiveBigIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    delivery_token = models.UUIDField(default=uuid4)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chat", "ordinal"), name="chat_turn_ordinal"
            ),
            models.UniqueConstraint(
                fields=("chat",),
                condition=models.Q(status__in=("queued", "running")),
                name="chat_one_active_turn",
            ),
        ]
        indexes = [models.Index(fields=("case", "status", "-created_at"))]

    def __str__(self):
        return f"Chat reply {self.ordinal} · {self.pk}"


class ChatSource(models.Model):
    """One versioned surface of a retained record used in a turn."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    turn = models.ForeignKey(ChatTurn, related_name="sources", on_delete=models.CASCADE)
    key = models.CharField(max_length=256)
    record_key = models.CharField(max_length=256)
    locator = models.JSONField()
    evidence_kind = models.CharField(max_length=32, blank=True)
    evidence_id = models.UUIDField(null=True)
    result_id = models.UUIDField(null=True)
    status = models.CharField(max_length=12, default="pending")
    cursor = models.PositiveBigIntegerField(default=0)
    gap = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("turn", "key"), name="chat_unique_source"),
        ]
        indexes = [
            models.Index(fields=("turn", "status", "id")),
            models.Index(fields=("turn", "record_key")),
        ]

    def __str__(self):
        return f"Chat source {self.pk}"


class ChatMatch(models.Model):
    """One exact, validated match in the full review ledger."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    turn = models.ForeignKey(ChatTurn, related_name="hits", on_delete=models.CASCADE)
    source = models.ForeignKey(
        ChatSource, related_name="hits", on_delete=models.CASCADE
    )
    start = models.PositiveBigIntegerField()
    end = models.PositiveBigIntegerField()
    quote = models.TextField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source", "start", "end"), name="chat_unique_match"
            ),
            models.CheckConstraint(
                condition=models.Q(end__gt=models.F("start")), name="chat_match_range"
            ),
        ]

    def __str__(self):
        return f"Chat match {self.pk}"

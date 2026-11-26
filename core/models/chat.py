"""Saved case conversations and checkpointed, source-bound answers."""

from uuid import uuid4

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from django.db.models.fields.json import KT
from django.db.models.functions import Left


class ChatRoutingApproval(models.Model):
    """The latest owner decision for the optional external source selection."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.OneToOneField("cases.Case", on_delete=models.CASCADE)
    destination = models.JSONField(default=dict)
    revision = models.UUIDField(default=uuid4)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    changed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Chat routing approval for case {self.case_id}"


class EvidenceChat(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    title = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


def chat_topic_vector():
    """Search the compact part of a saved reply that can be recalled as context."""
    return SearchVector(
        Left("question", 300),
        Left(KT("answer__clarification"), 400),
        Left(KT("answer__lead"), 200),
        Left(KT("answer__claims__0__text"), 400),
        Left(KT("answer__claims__1__text"), 400),
        config="simple",
    )


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
    review_of = models.OneToOneField(
        "self", null=True, related_name="deeper_review", on_delete=models.CASCADE
    )
    review_requests = models.PositiveIntegerField(
        default=0, db_default=0, editable=False
    )
    ordinal = models.PositiveIntegerField()
    question = models.TextField(max_length=2000)
    include_archived = models.BooleanField(default=False)
    submission_id = models.UUIDField()
    status = models.CharField(max_length=16, choices=Status, default=Status.QUEUED)
    mode = models.CharField(max_length=20, blank=True)
    execution = models.JSONField(default=dict)
    approval_revision = models.CharField(max_length=64, blank=True)
    rerank_started_at = models.DateTimeField(null=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    answer = models.JSONField(default=dict)
    plan = models.JSONField(default=dict)
    model_usage = models.JSONField(default=list, db_default=[], editable=False)
    activity = models.JSONField(default=dict, db_default={}, editable=False)
    total = models.PositiveBigIntegerField(default=0)
    reviewed = models.PositiveBigIntegerField(default=0)
    gaps = models.PositiveBigIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    delivery_token = models.UUIDField(default=uuid4)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("case", "asked_by", "submission_id"),
                name="chat_turn_submission_scope",
            ),
            models.UniqueConstraint(
                fields=("chat", "ordinal"), name="chat_turn_ordinal"
            ),
            models.UniqueConstraint(
                fields=("chat",),
                condition=models.Q(status__in=("queued", "running")),
                name="chat_one_active_turn",
            ),
        ]
        indexes = [
            models.Index(fields=("case", "status", "-created_at")),
            models.Index(
                fields=("chat", "case", "-created_at"), name="chat_turn_latest_created"
            ),
            GinIndex(
                chat_topic_vector(),
                condition=models.Q(status="complete"),
                name="chat_completed_topics",
            ),
        ]

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
            models.Index(
                fields=("evidence_kind", "evidence_id"), name="chat_source_evidence"
            ),
            models.Index(fields=("result_id",), name="chat_source_result"),
        ]

    def __str__(self):
        return f"Chat source {self.pk}"


class ChatSelectionMember(models.Model):
    """An original included in a recorded query, with its frozen clock precision."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    turn = models.ForeignKey(
        ChatTurn, related_name="selection_members", on_delete=models.CASCADE
    )
    kind = models.CharField(max_length=32)
    evidence_id = models.UUIDField()
    event_at = models.DateTimeField(null=True)
    date_only = models.BooleanField(default=False)
    forwarded = models.BooleanField(default=False)
    source_version = models.BigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("turn", "kind", "evidence_id"), name="chat_selection_member"
            ),
        ]
        indexes = [
            models.Index(fields=("kind", "evidence_id"), name="chat_selected_evidence"),
            models.Index(fields=("turn", "event_at"), name="chat_selected_clock"),
        ]

    def __str__(self):
        return f"{self.kind} {self.evidence_id} in selection {self.turn_id}"


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


class ChatAnalysisProposal(models.Model):
    """One investigator-reviewed PDF extraction and its linked answer."""

    class Status(models.TextChoices):
        OFFERED = "offered", "Awaiting approval"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        STOPPED = "stopped", "Needs attention"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    parent = models.OneToOneField(
        ChatTurn, related_name="analysis_proposal", on_delete=models.CASCADE
    )
    document = models.ForeignKey("document.DocumentEvidence", on_delete=models.CASCADE)
    sha256 = models.CharField(max_length=64)
    method_version = models.CharField(max_length=64)
    purpose = models.CharField(max_length=300)
    context = models.JSONField(default=list)
    status = models.CharField(max_length=12, choices=Status, default=Status.OFFERED)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )
    run = models.ForeignKey("core.AnalysisRun", null=True, on_delete=models.CASCADE)
    continuation = models.OneToOneField(
        ChatTurn, related_name="approved_check", null=True, on_delete=models.CASCADE
    )
    error_code = models.CharField(max_length=64, blank=True)

    def __str__(self):
        return f"Proposed extraction for chat reply {self.parent_id}"

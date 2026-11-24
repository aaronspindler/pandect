"""Source-backed conversation sessions published by a completed grouping revision."""

from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class ConversationGrouping(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    batch = models.ForeignKey(
        "core.CaseAnalysisBatch",
        on_delete=models.CASCADE,
        related_name="conversation_groupings",
    )
    first_person = models.ForeignKey(
        "people.Person", on_delete=models.CASCADE, related_name="+"
    )
    second_person = models.ForeignKey(
        "people.Person", on_delete=models.CASCADE, related_name="+"
    )
    parameters = models.JSONField(default=dict)
    method_version = models.CharField(max_length=64)
    source_count = models.PositiveBigIntegerField()
    scope_digest = models.CharField(max_length=64)
    processed_count = models.PositiveBigIntegerField(default=0)
    processed_digest = models.CharField(max_length=64, blank=True)
    cursor = models.JSONField(default=dict)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "first_person", "second_person"),
                name="phone_grouping_batch_pair",
            ),
            models.CheckConstraint(
                condition=models.Q(first_person_id__lte=models.F("second_person_id")),
                name="phone_grouping_pair_order",
            ),
        ]
        indexes = [
            models.Index(
                fields=("case", "first_person", "second_person", "published_at")
            )
        ]

    def __str__(self):
        return f"Conversation grouping {self.pk}"

    def save(self, *args, **kwargs):
        if self._state.adding:
            if any(
                obj.case_id != self.case_id
                for obj in (self.batch, self.first_person, self.second_person)
            ):
                raise ValidationError(
                    "Conversation grouping references must share a case."
                )
            kwargs["force_insert"] = True
        else:
            progress = {
                "cursor",
                "processed_count",
                "processed_digest",
                "published_at",
                "published_by",
            }
            changes = kwargs.get("update_fields")
            if not changes or set(changes) - progress:
                raise ValidationError(
                    "The sources and settings of a grouping revision are retained."
                )
            if (
                type(self)
                .objects.filter(pk=self.pk, published_at__isnull=False)
                .exists()
            ):
                raise ValidationError(
                    "A published conversation grouping cannot be replaced."
                )
        return super().save(*args, **kwargs)


class ConversationGroupingRequest(models.Model):
    """Each authorized batch can resume the shared exact revision it requested."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    batch = models.ForeignKey(
        "core.CaseAnalysisBatch",
        on_delete=models.CASCADE,
        related_name="grouping_requests",
    )
    grouping = models.ForeignKey(
        ConversationGrouping, on_delete=models.CASCADE, related_name="requests"
    )
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "grouping"), name="phone_grouping_request_once"
            )
        ]

    def __str__(self):
        return f"Conversation grouping request {self.pk}"

    def save(self, *args, **kwargs):
        if self.batch.case_id != self.case_id or self.grouping.case_id != self.case_id:
            raise ValidationError(
                "Grouping requests must belong to their batch and revision's case."
            )
        if self._state.adding:
            kwargs["force_insert"] = True
        elif set(kwargs.get("update_fields") or ()) != {"error_code"}:
            raise ValidationError("The original grouping request is retained.")
        return super().save(*args, **kwargs)


class ConversationSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    grouping = models.ForeignKey(
        ConversationGrouping, on_delete=models.CASCADE, related_name="sessions"
    )
    stream_key = models.CharField(max_length=64)
    start_ordinal = models.PositiveBigIntegerField()
    boundary_reason = models.CharField(max_length=255)
    first_at = models.DateTimeField(null=True, blank=True)
    last_at = models.DateTimeField(null=True, blank=True)
    message_count = models.PositiveBigIntegerField(default=0)
    membership_digest = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("grouping", "start_ordinal"),
                name="phone_session_grouping_order",
            )
        ]
        indexes = [
            models.Index(fields=("grouping", "stream_key", "start_ordinal")),
            models.Index(fields=("case", "first_at", "membership_digest")),
        ]

    def __str__(self):
        return f"Conversation session {self.pk}"

    def save(self, *args, **kwargs):
        if self.grouping.case_id != self.case_id or self.grouping.published_at:
            raise ValidationError("Choose an unpublished grouping in this case.")
        if not self._state.adding:
            raise ValidationError(
                "Session changes are only made by grouping preparation."
            )
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)


class ConversationSessionMember(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey("cases.Case", on_delete=models.CASCADE)
    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="members"
    )
    message = models.ForeignKey("phone.TextMessageEvidence", on_delete=models.RESTRICT)
    ordinal = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    context_id_snapshot = models.UUIDField(null=True)
    continuation_reason = models.CharField(max_length=255, blank=True)
    parent_message_id = models.UUIDField(null=True, blank=True)
    elapsed_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("session", "ordinal"), name="phone_session_member_order"
            ),
            models.UniqueConstraint(
                fields=("session", "message"), name="phone_session_message_once"
            ),
        ]

    def __str__(self):
        return f"Session message {self.ordinal}"

    def save(self, *args, **kwargs):
        if not self._state.adding or self.session.grouping.published_at:
            raise ValidationError("Saved session membership cannot be replaced.")
        if self.message.case_id != self.case_id or self.session.case_id != self.case_id:
            raise ValidationError("Session membership must share its original's case.")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

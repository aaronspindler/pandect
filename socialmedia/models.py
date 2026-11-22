from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone

from socialmedia.instagram_rosters import LIVE_VERSION


class SocialAccount(models.Model):
    """A reported platform and username associated with a person."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    person = models.ForeignKey(
        "people.Person", on_delete=models.CASCADE, related_name="social_accounts"
    )
    platform = models.CharField(max_length=80)
    username = models.CharField(max_length=150)
    source = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("platform", "username", "pk")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(platform__regex=r"\S", username__regex=r"\S"),
                name="people_social_account_details_present",
            ),
            models.UniqueConstraint(
                fields=("person", "platform", "username"),
                name="people_social_account_unique",
            ),
        ]

    def __str__(self):
        return f"{self.platform} · {self.username}"


class InstagramSource(models.TextChoices):
    SYNTHETIC = "synthetic", "Synthetic Instagram"
    INSTAGRAM = "instagram", "Instagram"


class InstagramTargetApproval(models.Model):
    """A case owner authorized collection for one account at a specific source."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="instagram_target_approvals",
    )
    source = models.CharField(max_length=9, choices=InstagramSource)
    target_id = models.CharField(max_length=128)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(source__in=InstagramSource.values)
                & ~models.Q(target_id=""),
                name="social_target_approval_scope",
            ),
            models.UniqueConstraint(
                fields=("case", "source", "target_id"),
                name="social_target_approval_unique",
            ),
        ]

    def __str__(self):
        return f"{self.get_source_display()} · {self.target_id}"


class InstagramConnection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="instagram_connection",
    )
    source = models.CharField(
        max_length=9, choices=InstagramSource, default=InstagramSource.SYNTHETIC
    )
    observer = models.CharField(max_length=30, blank=True)
    observer_id_string = models.CharField(max_length=128, blank=True)
    observer_epoch = models.UUIDField(default=uuid4, editable=False)
    session = models.TextField(blank=True, editable=False)
    cooldown_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(source__in=InstagramSource.values),
                name="social_connection_source",
            ),
        ]

    def __str__(self):
        return self.observer or "Disconnected Instagram account"


class InstagramMonitor(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        STOPPED = "stopped", "Stopped"

    INTERVALS = (
        (1, "Every hour"),
        (6, "Every 6 hours"),
        (12, "Every 12 hours"),
        (24, "Every 24 hours"),
    )
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    case = models.ForeignKey(
        "cases.Case", on_delete=models.CASCADE, related_name="instagram_monitors"
    )
    connection = models.ForeignKey(
        InstagramConnection, on_delete=models.PROTECT, related_name="monitors"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    source = models.CharField(
        max_length=9,
        choices=InstagramSource,
        default=InstagramSource.SYNTHETIC,
        editable=False,
    )
    target = models.CharField(max_length=30)
    target_id = models.CharField(max_length=128, blank=True, editable=False)
    interval_hours = models.PositiveSmallIntegerField(choices=INTERVALS, default=24)
    status = models.CharField(max_length=7, choices=Status, default=Status.ACTIVE)
    generation = models.UUIDField(default=uuid4, editable=False)
    next_check_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(source__in=InstagramSource.values),
                name="social_monitor_source",
            ),
            models.CheckConstraint(
                condition=models.Q(interval_hours__in=(1, 6, 12, 24)),
                name="social_monitor_interval",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=("active", "paused", "stopped")),
                name="social_monitor_status",
            ),
        ]

    def __str__(self):
        return f"@{self.target}"


class InstagramCheck(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    monitor = models.ForeignKey(
        InstagramMonitor, on_delete=models.CASCADE, related_name="checks"
    )
    generation = models.UUIDField(default=uuid4, editable=False)
    status = models.CharField(max_length=9, choices=Status, default=Status.RUNNING)
    error = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-started_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=("monitor", "generation"), name="social_check_delivery"
            ),
        ]

    def __str__(self):
        return f"Instagram check · {self.started_at.isoformat()}"


class InstagramCapture(models.Model):
    class Direction(models.TextChoices):
        FOLLOWERS = "followers", "Followers"
        FOLLOWING = "following", "Accounts followed"

    class Reason(models.TextChoices):
        STALLED = "stalled", "The list stopped loading"
        MISMATCH = (
            "count_mismatch",
            "The visible count did not match the collected list",
        )
        TIME = "time_limit", "The collection time limit was reached"
        ROWS = "row_limit", "The collection size limit was reached"
        ACCESS = "access_lost", "Access to the list was lost"
        UNAVAILABLE = "unavailable", "The list was unavailable"
        END = "ambiguous_end", "The end of the list could not be verified"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    monitor_check = models.ForeignKey(
        InstagramCheck, on_delete=models.CASCADE, related_name="captures"
    )
    direction = models.CharField(max_length=9, choices=Direction)
    evidence = models.OneToOneField(
        "document.DocumentEvidence",
        on_delete=models.CASCADE,
        related_name="instagram_capture",
    )
    observer_epoch = models.UUIDField(editable=False)
    target_identity = models.CharField(max_length=140)
    collector_version = models.CharField(max_length=64)
    complete = models.BooleanField()
    reason = models.CharField(max_length=16, blank=True, choices=Reason)
    entries = models.JSONField(default=list)
    entry_count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("monitor_check", "direction"), name="social_capture_direction"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(complete=True, reason="")
                    | (models.Q(complete=False) & ~models.Q(reason=""))
                ),
                name="social_capture_coverage",
            ),
        ]

    def __str__(self):
        return f"{self.get_direction_display()} · {self.entry_count}"

    @property
    def baseline_eligible(self):
        # This endpoint collector emits count_mismatch only after a terminal page.
        return self.complete or (
            self.collector_version == LIVE_VERSION
            and self.reason == self.Reason.MISMATCH
        )


class InstagramComparison(models.Model):
    """Deleting either capture removes just the comparison that used it."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    capture = models.OneToOneField(
        InstagramCapture, on_delete=models.CASCADE, related_name="comparison"
    )
    previous = models.ForeignKey(
        InstagramCapture, on_delete=models.CASCADE, related_name="later_comparisons"
    )
    additions = models.JSONField(default=list)
    addition_count = models.PositiveIntegerField()

    def __str__(self):
        return f"Instagram comparison · {self.addition_count} additions"

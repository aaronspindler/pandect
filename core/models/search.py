"""Private Cloudflare destinations, delivery cursors and canonical passage receipts."""

from uuid import uuid4

from django.db import models


class HostedSearchIndex(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    # A deletion receipt has to outlive the case. It contains no source data.
    case_key = models.UUIDField(db_index=True)
    revision = models.UUIDField(unique=True)
    account = models.CharField(max_length=32)
    namespace = models.CharField(max_length=64)
    provisioned = models.BooleanField(default=False)
    retired = models.BooleanField(default=False)
    phase = models.PositiveSmallIntegerField(default=0)
    cursor = models.UUIDField(null=True)
    status = models.CharField(max_length=12, default="queued")
    token = models.UUIDField(default=uuid4)
    failures = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=40, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["case_key"],
                condition=models.Q(retired=False),
                name="hosted_one_live_index",
            )
        ]

    def __str__(self):
        return f"Private case index {self.id}"

    @property
    def instance(self):
        return "pc-" + self.id.hex


class HostedSearchSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    index = models.ForeignKey(HostedSearchIndex, on_delete=models.CASCADE)
    surface = models.CharField(max_length=16)
    kind = models.CharField(max_length=24, blank=True)
    source_key = models.UUIDField()
    dirty = models.BooleanField(default=True)
    offset = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["index", "surface", "kind", "source_key"],
                name="hosted_source_identity",
            ),
        ]
        indexes = [
            models.Index(
                fields=["index", "id"],
                condition=models.Q(dirty=True),
                name="hosted_dirty_sources",
            ),
        ]

    def __str__(self):
        return f"{self.surface} {self.source_key}"


class HostedSearchItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    source = models.ForeignKey(HostedSearchSource, on_delete=models.CASCADE)
    version = models.CharField(max_length=64)
    start = models.PositiveIntegerField()
    length = models.PositiveSmallIntegerField()
    digest = models.CharField(max_length=64)
    available_at = models.DateTimeField()
    event_start = models.DateTimeField()
    event_end = models.DateTimeField()
    remote_id = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=10, default="queued")
    checked_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "version", "start", "event_start", "event_end"],
                name="hosted_item_window",
            ),
        ]
        indexes = [models.Index(fields=["status", "checked_at"])]

    def __str__(self):
        return f"Search passage {self.id}"

    @property
    def filename(self):
        return self.id.hex + ".txt"

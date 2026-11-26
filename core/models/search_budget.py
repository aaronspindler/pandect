from uuid import uuid4

from django.db import models


class CloudflareSearchRateLimit(models.Model):
    """Shared AI Search request budget with no case information or credentials."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    account = models.CharField(max_length=32, unique=True)
    scheduled_at = models.DateTimeField()
    resume_at = models.DateTimeField(null=True)

    def __str__(self):
        return f"Cloudflare AI Search budget {self.pk}"
